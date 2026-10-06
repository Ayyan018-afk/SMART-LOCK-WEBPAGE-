"""SmartLock Delivery Portal (Streamlit + SQLite + HTML/CSS)

Setup:  pip install "streamlit>=1.32" "qrcode[pil]"
Run:    streamlit run smartlock.py
Data is stored in smartlock_v3.db next to this file (delete it to reset).
"""
import sqlite3, hashlib, hmac, secrets, io, re, os
from datetime import datetime
from html import escape as esc
import pandas as pd
import streamlit as st
import qrcode

# Optional: write a matching theme file once (applies from the next restart) and silence the email prompt.
try:
    if not os.path.exists(".streamlit/config.toml"):
        os.makedirs(".streamlit", exist_ok=True)
        with open(".streamlit/config.toml", "w") as f:
            f.write('[theme]\nbase="light"\nprimaryColor="#7c3aed"\nbackgroundColor="#f6f4ff"\n'
                    'secondaryBackgroundColor="#ffffff"\ntextColor="#1f2340"\n\n[browser]\ngatherUsageStats=false\n')
except OSError:
    pass

st.set_page_config(page_title="SmartLock Delivery", page_icon="🔐", layout="wide")
DB = "smartlock_v3.db"
ACTIVE = ("Waiting for box", "Placed", "Out for delivery", "In box")
FEE = {"Home Delivery": 200, "Smart Lock": 0}
PAY = ["Cash on Delivery", "Credit / Debit Card", "Easypaisa", "JazzCash", "Google Pay", "Bank Transfer"]
PAY_ICON = {"Cash on Delivery": "💵", "Credit / Debit Card": "💳", "Easypaisa": "📱", "JazzCash": "📲", "Google Pay": "🅖", "Bank Transfer": "🏦"}
ICONS = ["🎧", "👟", "🎒", "⌚", "📱", "🕶️", "📚", "👕", "🎮", "💡", "🧴", "📦"]


# ======================= database =======================
def q(sql, args=(), one=False, commit=False):
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        if commit:
            con.commit()
            return cur.lastrowid
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        con.close()


def hp(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100_000).hex()}"


def check_pw(pw, stored):
    return hmac.compare_digest(hp(pw, stored.split("$")[0]), stored)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def init():
    for s in [
        "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw TEXT, role TEXT, name TEXT)",
        "CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT, price REAL, seller_id INT, icon TEXT)",
        "CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS boxes(id INTEGER PRIMARY KEY, location_id INT, label TEXT, status TEXT DEFAULT 'free', locked INT DEFAULT 1)",
        """CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, customer_id INT, product_id INT, qty INT, method TEXT,
           status TEXT, location_id INT, box_id INT, drop_token TEXT, pick_token TEXT, rider_id INT, created TEXT,
           address TEXT, phone TEXT, pay_method TEXT, pay_status TEXT, pay_ref TEXT, subtotal REAL, fee REAL, total REAL)""",
        "CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INT, msg TEXT, ts TEXT)",
        "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, order_id INT, sender TEXT, msg TEXT, ts TEXT)",
        "CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY, customer_id INT, text TEXT, status TEXT DEFAULT 'Open', ts TEXT)",
    ]:
        q(s, commit=True)
    if not q("SELECT 1 FROM users", one=True):
        for u, p, r, n in [("admin", "admin123", "admin", "Admin"), ("customer", "cust123", "customer", "Demo Customer"),
                           ("rider", "rider123", "rider", "Demo Rider"), ("seller", "sell123", "seller", "Demo Seller")]:
            q("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)", (u, hp(p), r, n), commit=True)
        sid = q("SELECT id FROM users WHERE role='seller'", one=True)["id"]
        for n, p, i in [("Wireless Earbuds", 4500, "🎧"), ("Running Shoes", 7800, "👟"), ("Backpack", 3200, "🎒"),
                        ("Smart Watch", 12500, "⌚"), ("Sunglasses", 2800, "🕶️"), ("Notebook Set", 950, "📚")]:
            q("INSERT INTO products(name,price,seller_id,icon) VALUES(?,?,?,?)", (n, p, sid, i), commit=True)
        for loc, nb in [("Gulshan Hub", 3), ("Clifton Hub", 2), ("DHA Hub", 1)]:
            lid = q("INSERT INTO locations(name) VALUES(?)", (loc,), commit=True)
            for i in range(nb):
                q("INSERT INTO boxes(location_id,label) VALUES(?,?)", (lid, f"{loc[:3].upper()}-{i + 1}"), commit=True)


init()


# ======================= business logic =======================
def notify(uid, msg):
    q("INSERT INTO notifications(user_id,msg,ts) VALUES(?,?,?)", (uid, msg, now()), commit=True)


def qr_png(data):
    buf = io.BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()


def new_token():
    return secrets.token_hex(4).upper()


def fill_waiting(loc_id):
    while True:
        box = q("SELECT * FROM boxes WHERE location_id=? AND status='free' LIMIT 1", (loc_id,), one=True)
        o = q("SELECT * FROM orders WHERE location_id=? AND status='Waiting for box' ORDER BY id LIMIT 1", (loc_id,), one=True)
        if not box or not o:
            return
        q("UPDATE boxes SET status='reserved' WHERE id=?", (box["id"],), commit=True)
        q("UPDATE orders SET box_id=?, status='Placed' WHERE id=?", (box["id"], o["id"]), commit=True)
        notify(o["customer_id"], f"Box {box['label']} is now reserved for your order #{o['id']}.")


def release_box(box_id):
    box = q("SELECT * FROM boxes WHERE id=?", (box_id,), one=True)
    q("UPDATE boxes SET status='free', locked=1 WHERE id=?", (box_id,), commit=True)
    fill_waiting(box["location_id"])


def block_box(box_id):
    box = q("SELECT * FROM boxes WHERE id=?", (box_id,), one=True)
    o = q("SELECT * FROM orders WHERE box_id=? AND status IN ('Placed','Out for delivery')", (box_id,), one=True)
    q("UPDATE boxes SET status='blocked', locked=1 WHERE id=?", (box_id,), commit=True)
    if o:
        q("UPDATE orders SET status='Waiting for box', box_id=NULL, rider_id=NULL WHERE id=?", (o["id"],), commit=True)
        notify(o["customer_id"], f"Box {box['label']} was blocked. Order #{o['id']} is waiting for another box.")
        fill_waiting(box["location_id"])


def cancel_order(o):
    pay = "Refund pending" if o["pay_status"] == "Paid" else "Cancelled"
    q("UPDATE orders SET status='Cancelled', pay_status=? WHERE id=?", (pay, o["id"]), commit=True)
    if o["box_id"]:
        release_box(o["box_id"])


ORDER_SQL = """SELECT o.*, p.name pname, p.icon picon, b.label blabel, l.name lname FROM orders o
               JOIN products p ON p.id=o.product_id LEFT JOIN boxes b ON b.id=o.box_id
               LEFT JOIN locations l ON l.id=o.location_id"""


# ======================= HTML / CSS =======================
CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap');
html,body,.stApp,button,input,textarea{font-family:'Poppins',sans-serif}
.stApp{background:linear-gradient(160deg,#f6f4ff 0%,#eef5ff 55%,#effcf8 100%)}
.block-container{padding-top:1.6rem;max-width:1200px}
.stApp p,.stApp label,.stApp li,.stApp h1,.stApp h2,.stApp h3,.stApp h4{color:#1f2340}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e9e5ff}
.stButton>button,.stFormSubmitButton>button{border-radius:10px;font-weight:600;border:1px solid #d9ccff}
button[kind="primary"],button[data-testid="stBaseButton-primary"],button[kind="primaryFormSubmit"]{background:linear-gradient(135deg,#7c3aed,#ec4899)!important;border:0!important}
button[kind="primary"] p,button[data-testid="stBaseButton-primary"] p,button[kind="primaryFormSubmit"] p{color:#fff!important}
button[kind="primary"]:hover,button[data-testid="stBaseButton-primary"]:hover{filter:brightness(1.08);transform:translateY(-1px)}
[data-testid="stTextInput"] div[data-baseweb="input"] button{display:none!important}
.stTabs [data-baseweb="tab"]{font-weight:600;padding:10px 16px}
.stTabs [aria-selected="true"],.stTabs [aria-selected="true"] p{color:#7c3aed!important}
.stTabs [data-baseweb="tab-highlight"]{background:#7c3aed}
[data-testid="stMetric"]{background:#fff;border-radius:16px;padding:14px 18px;box-shadow:0 4px 18px #7c3aed14;border:1px solid #efeaff}
[data-testid="stExpander"]{background:#fff;border-radius:14px;border:1px solid #ece7ff}
.stApp .hero{background:linear-gradient(120deg,#4f46e5 0%,#7c3aed 50%,#ec4899 100%);padding:26px 30px;border-radius:20px;margin-bottom:20px;box-shadow:0 12px 30px #7c3aed33}
.stApp .hero h1{margin:0;font-size:2rem;font-weight:800;color:#fff}
.stApp .hero p{margin:6px 0 0;color:#ffffffd9;font-size:1rem}
.big{font-size:2.6rem!important;line-height:1.15}
.feats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0}
.feat{background:#fff;border-radius:16px;padding:16px;box-shadow:0 4px 18px #7c3aed14;border:1px solid #efeaff}
.feat .ic{font-size:1.7rem}.feat b{display:block;color:#1f2340;margin-top:4px}.feat span{color:#6b7280;font-size:.85rem}
.steps3{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.steps3 div{flex:1;min-width:150px;background:#ffffffb0;border-radius:14px;padding:12px;color:#1f2340;font-size:.88rem;border:1px dashed #c4b5fd}
.pcard{background:#fff;border-radius:18px;padding:18px;text-align:center;box-shadow:0 6px 22px #7c3aed1a;border:1px solid #efeaff;margin-bottom:8px}
.pcard .em{font-size:3rem;background:linear-gradient(135deg,#ede9fe,#fce7f3);border-radius:16px;padding:10px 0;margin-bottom:10px}
.pcard .nm{font-weight:600;color:#1f2340}.pcard .pr{font-weight:700;color:#7c3aed;font-size:1.1rem}
.badge{display:inline-block;padding:3px 11px;border-radius:999px;font-size:.78rem;font-weight:600;background:#e5e7eb;color:#374151}
.badge.placed{background:#dbeafe;color:#1d4ed8}.badge.out-for-delivery{background:#fef3c7;color:#b45309}
.badge.in-box{background:#ede9fe;color:#6d28d9}.badge.completed,.badge.paid{background:#dcfce7;color:#15803d}
.badge.cancelled,.badge.refund-pending{background:#fee2e2;color:#b91c1c}.badge.waiting-for-box,.badge.pending-verification,.badge.pay-on-delivery{background:#ffedd5;color:#c2410c}
.tl{display:flex;margin:14px 0 6px}.step{flex:1;text-align:center;position:relative}
.step:not(:last-child)::after{content:"";position:absolute;top:15px;left:50%;width:100%;height:3px;background:#e5e7eb;z-index:0}
.step.done:not(:last-child)::after{background:#7c3aed}
.dot{width:32px;height:32px;line-height:32px;border-radius:50%;background:#e5e7eb;color:#6b7280;margin:0 auto;font-weight:700;position:relative;z-index:1}
.step.done .dot{background:#7c3aed;color:#fff}.step.cur .dot{background:#ec4899;color:#fff;box-shadow:0 0 0 5px #ec489933}
.lbl{font-size:.78rem;margin-top:6px;color:#4b5563;font-weight:500}
.sum{background:#fff;border-radius:18px;padding:20px;box-shadow:0 8px 26px #7c3aed1a;border:1px solid #efeaff;color:#1f2340}
.sum .row{display:flex;justify-content:space-between;padding:6px 0;color:#374151}
.sum .tot{border-top:2px dashed #ddd6fe;margin-top:8px;padding-top:10px;font-weight:700;font-size:1.15rem;color:#7c3aed}
.note{background:#fff;border-left:4px solid #7c3aed;border-radius:10px;padding:10px 14px;margin-bottom:8px;color:#1f2340;box-shadow:0 2px 10px #0000000a}
.note small{color:#9ca3af}
.rec{width:100%;border-collapse:collapse;font-size:.88rem}.rec td{padding:6px 4px;border-bottom:1px solid #f1f0f7;color:#1f2340}.rec td:first-child{color:#6b7280;width:38%}
.footer{text-align:center;color:#6b7280;font-size:.82rem;margin-top:40px;padding:18px 0;border-top:1px solid #e9e5ff}
@media(max-width:800px){.feats{grid-template-columns:1fr}.big{font-size:1.9rem!important}}
</style>"""


def H(s):
    st.markdown("".join(l.strip() for l in s.splitlines()), unsafe_allow_html=True)


def hero(sub, title="🔐 SmartLock Delivery"):
    H(f'<div class="hero"><h1>{title}</h1><p>{sub}</p></div>')


def badge(s):
    return f'<span class="badge {re.sub(r"[^a-z]+", "-", str(s).lower()).strip("-")}">{esc(str(s))}</span>'


def timeline(o):
    steps = ["Placed", "Out for delivery", "In box", "Completed"] if o["method"] == "Smart Lock" else ["Placed", "Out for delivery", "Completed"]
    if o["status"] == "Cancelled":
        return badge("Cancelled")
    idx = steps.index(o["status"]) if o["status"] in steps else -1
    done_all = o["status"] == "Completed"
    out = ""
    for i, s in enumerate(steps):
        done = done_all or i < idx
        cls = "done" if done else ("cur" if i == idx else "")
        out += f'<div class="step {cls}"><div class="dot">{"✓" if done else i + 1}</div><div class="lbl">{s}</div></div>'
    note = '<p style="color:#c2410c;font-size:.85rem">⏳ Waiting for a free box at your chosen location.</p>' if o["status"] == "Waiting for box" else ""
    return f'<div class="tl">{out}</div>{note}'


def receipt(o):
    dest = f"📍 {esc(o['lname'] or '')} · Box {esc(o['blabel'] or 'pending')}" if o["method"] == "Smart Lock" else f"🏠 {esc(o['address'] or '')}"
    rows = [("Item", f"{esc(o['picon'] or '')} {esc(o['pname'])} × {o['qty']}"), ("Delivery", f"{o['method']} — {dest}"),
            ("Contact", esc(o["phone"] or "")), ("Payment", f"{PAY_ICON.get(o['pay_method'], '')} {esc(o['pay_method'] or '')}"),
            ("Payment status", badge(o["pay_status"])), ("Reference", esc(o["pay_ref"] or "—")),
            ("Subtotal", f"Rs {o['subtotal']:,.0f}"), ("Delivery fee", f"Rs {o['fee']:,.0f}"), ("<b>Total</b>", f"<b>Rs {o['total']:,.0f}</b>")]
    return '<table class="rec">' + "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows) + "</table>"


def footer():
    H('<div class="footer">🔐 SmartLock Delivery · Secure QR parcel boxes · Group project © 2026</div>')


# ======================= login =======================
def login_page():
    left, right = st.columns([1.25, 1], gap="large")
    with left:
        H("""<div class="hero"><h1 class="big">Your parcel.<br>Your QR. Your time.</h1>
        <p>SmartLock Delivery stores your order in a secure box and opens it only for you — no missed deliveries, no waiting at home.</p></div>
        <div class="feats">
        <div class="feat"><div class="ic">🔐</div><b>Secure QR access</b><span>Only the rider and the customer can open a box.</span></div>
        <div class="feat"><div class="ic">📍</div><b>Live tracking</b><span>Follow every order from placed to picked up.</span></div>
        <div class="feat"><div class="ic">⏰</div><b>Pick up anytime</b><span>Collect your parcel when it suits you.</span></div></div>
        <div class="steps3"><div><b>1.</b> Choose Smart Lock at checkout</div><div><b>2.</b> Rider drops it in your box</div>
        <div><b>3.</b> Scan your QR and collect</div></div>""")
    with right:
        with st.container(border=True):
            st.markdown("### Welcome 👋")
            t1, t2 = st.tabs(["Login", "Sign up"])
            with t1:
                role = st.selectbox("Login as", ["customer", "rider", "seller", "admin"], format_func=str.title)
                u = st.text_input("Username", key="lu")
                p = st.text_input("Password", type="password", key="lp")
                if st.button("Login", type="primary"):
                    r = q("SELECT * FROM users WHERE username=? AND role=?", (u.strip().lower(), role), one=True)
                    if r and check_pw(p, r["pw"]):
                        st.session_state.user = dict(r)
                        st.rerun()
                    else:
                        st.error("Invalid username, password or role.")
            with t2:
                n = st.text_input("Full name", key="sn")
                u2 = st.text_input("Username", key="su")
                p2 = st.text_input("Password (min 6 characters)", type="password", key="sp")
                r2 = st.selectbox("Role", ["customer", "rider", "seller"], key="sr", format_func=str.title)
                if st.button("Create account", type="primary"):
                    u2 = u2.strip().lower()
                    if not n.strip() or not u2 or len(p2) < 6:
                        st.warning("Fill all fields; password must be at least 6 characters.")
                    elif not re.fullmatch(r"[a-z0-9_.]{3,20}", u2):
                        st.warning("Username: 3-20 letters, numbers, _ or .")
                    elif q("SELECT 1 FROM users WHERE username=?", (u2,), one=True):
                        st.error("Username already taken.")
                    else:
                        q("INSERT INTO users(username,pw,role,name) VALUES(?,?,?,?)", (u2, hp(p2), r2, n.strip()), commit=True)
                        st.success("Account created. Please log in.")


# ======================= chat =======================
def send_msg(order_id, sender):
    k = f"m{order_id}"
    text = st.session_state.get(k, "").strip()
    if text:
        q("INSERT INTO messages(order_id,sender,msg,ts) VALUES(?,?,?,?)", (order_id, sender, text, now()), commit=True)
    st.session_state[k] = ""


def chat_box(order_id, sender):
    for m in q("SELECT * FROM messages WHERE order_id=? ORDER BY id", (order_id,)):
        H(f'<div class="note"><b>{esc(m["sender"])}</b> <small>{m["ts"]}</small><br>{esc(m["msg"])}</div>')
    st.text_input("Message", key=f"m{order_id}")
    st.button("Send", key=f"s{order_id}", on_click=send_msg, args=(order_id, sender))


# ======================= customer =======================
def pay_details(pm):
    """Validate payment inputs. Returns (error, reference). Sensitive details are never stored."""
    g = lambda k: st.session_state.get(k, "").strip()
    tx = "TX-" + new_token()
    if pm == "Cash on Delivery":
        return None, "Cash on delivery"
    if pm == "Credit / Debit Card":
        num = re.sub(r"\s", "", g("cn"))
        if not re.fullmatch(r"\d{16}", num):
            return "Enter a valid 16-digit card number.", None
        if not g("cname"):
            return "Enter the name on the card.", None
        m = re.fullmatch(r"(\d{2})/(\d{2})", g("cexp"))
        if not m or not 1 <= int(m[1]) <= 12:
            return "Expiry must be in MM/YY format.", None
        n = datetime.now()
        if (2000 + int(m[2]), int(m[1])) < (n.year, n.month):
            return "This card has expired.", None
        if not re.fullmatch(r"\d{3,4}", g("ccvv")):
            return "Enter a valid CVV (3 or 4 digits).", None
        brand = "Visa" if num[0] == "4" else "Mastercard" if num[0] == "5" else "Card"
        return None, f"{brand} ending {num[-4:]} · {tx}"
    if pm in ("Easypaisa", "JazzCash"):
        w = g("wnum")
        if not re.fullmatch(r"03\d{9}", w):
            return f"Enter your {pm} mobile number (03XXXXXXXXX).", None
        if not re.fullmatch(r"\d{4,5}", g("wpin")):
            return f"Enter your {pm} PIN (4-5 digits).", None
        return None, f"{pm} {w[:4]}•••{w[-3:]} · {tx}"
    if pm == "Google Pay":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", g("gmail")):
            return "Enter a valid Google account email.", None
        return None, f"Google Pay {g('gmail')} · {tx}"
    if pm == "Bank Transfer":
        t = g("btid").upper()
        if not re.fullmatch(r"[A-Z0-9]{6,20}", t):
            return "Enter the bank transaction ID (6-20 letters/numbers).", None
        return None, f"Bank TID {t}"
    return "Choose a payment method.", None


def checkout(u):
    p = st.session_state.buy
    if st.button("← Back to shop"):
        del st.session_state.buy
        st.rerun()
    left, right = st.columns([1.5, 1], gap="large")
    with left:
        st.markdown(f"### {esc(p['icon'] or '')} Checkout — {esc(p['name'])}")
        st.markdown("#### 1 · Delivery details")
        qty = st.number_input("Quantity", 1, 10, 1, key="qty")
        dm = st.radio("How would you like to receive it?", ["Home Delivery", "Smart Lock"], horizontal=True, key="dm",
                      format_func=lambda x: "🏠 Home Delivery (Rs 200)" if x == "Home Delivery" else "🔐 Smart Lock Box (free)")
        st.text_input("Mobile number", placeholder="03XXXXXXXXX", key="phone")
        loc_id = None
        if dm == "Home Delivery":
            st.text_area("Delivery address", placeholder="House / street / area / city", key="addr")
        else:
            locs = q("SELECT * FROM locations")
            lname = st.selectbox("Pick-up location (smart lock hub)", [l["name"] for l in locs], key="loc")
            loc_id = next(l["id"] for l in locs if l["name"] == lname)
            n_free = len(q("SELECT 1 FROM boxes WHERE location_id=? AND status='free'", (loc_id,)))
            if n_free:
                st.success(f"✅ {n_free} box(es) available at {lname}.")
            else:
                st.warning("No box is free here right now. Choose another hub, or order and wait for a box to free up.")
        st.markdown("#### 2 · Payment method")
        options = PAY if dm == "Home Delivery" else PAY[1:]
        pm = st.radio("Pay with", options, key="pm", format_func=lambda x: f"{PAY_ICON[x]}  {x}")
        if dm == "Smart Lock":
            st.caption("Cash on Delivery isn't available for Smart Lock boxes — pay online.")
        if pm == "Cash on Delivery":
            st.info("Pay the rider in cash when your order arrives.")
        elif pm == "Credit / Debit Card":
            st.text_input("Card number", placeholder="1234 5678 9012 3456", max_chars=19, key="cn")
            st.text_input("Name on card", key="cname")
            c1, c2 = st.columns(2)
            c1.text_input("Expiry (MM/YY)", placeholder="08/28", max_chars=5, key="cexp")
            c2.text_input("CVV", type="password", max_chars=4, key="ccvv")
        elif pm in ("Easypaisa", "JazzCash"):
            st.text_input(f"{pm} mobile number", placeholder="03XXXXXXXXX", max_chars=11, key="wnum")
            st.text_input(f"{pm} PIN", type="password", max_chars=5, key="wpin")
        elif pm == "Google Pay":
            st.text_input("Google account email", placeholder="you@gmail.com", key="gmail")
        else:
            st.info("Transfer the total to **Demo Bank · SmartLock Delivery · IBAN PK00DEMO0000000000000001**, then enter the transaction ID. "
                    "An admin will verify it before dispatch.")
            st.text_input("Bank transaction ID", key="btid")
        st.caption("🔒 Demo payment — no real money moves and card/PIN details are never saved.")
    sub = p["price"] * qty
    fee = FEE[dm]
    total = sub + fee
    with right:
        H(f"""<div class="sum"><h3 style="margin-top:0">Order summary</h3>
        <div class="row"><span>{esc(p['icon'] or '')} {esc(p['name'])} × {qty}</span><span>Rs {sub:,.0f}</span></div>
        <div class="row"><span>Delivery fee</span><span>{"Free" if fee == 0 else f"Rs {fee:,.0f}"}</span></div>
        <div class="row tot"><span>Total</span><span>Rs {total:,.0f}</span></div></div>""")
        if st.button("Place order", type="primary"):
            phone = st.session_state.get("phone", "").strip()
            addr = st.session_state.get("addr", "").strip()
            err, ref = pay_details(pm)
            if not re.fullmatch(r"03\d{9}", phone):
                st.error("Enter a valid mobile number (03XXXXXXXXX).")
            elif dm == "Home Delivery" and len(addr) < 8:
                st.error("Enter your full delivery address.")
            elif err:
                st.error(err)
            else:
                box_id, status = None, "Placed"
                if dm == "Smart Lock":
                    b = q("SELECT * FROM boxes WHERE location_id=? AND status='free' LIMIT 1", (loc_id,), one=True)
                    if b:
                        box_id = b["id"]
                        q("UPDATE boxes SET status='reserved' WHERE id=?", (box_id,), commit=True)
                    else:
                        status = "Waiting for box"
                pay_status = "Pay on delivery" if pm == "Cash on Delivery" else "Pending verification" if pm == "Bank Transfer" else "Paid"
                oid = q("""INSERT INTO orders(customer_id,product_id,qty,method,status,location_id,box_id,drop_token,pick_token,created,
                           address,phone,pay_method,pay_status,pay_ref,subtotal,fee,total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (u["id"], p["id"], qty, dm, status, loc_id, box_id, new_token(), new_token(), now(),
                         addr if dm == "Home Delivery" else None, phone, pm, pay_status, ref, sub, fee, total), commit=True)
                notify(u["id"], f"Order #{oid} ({p['name']}) received — {pay_status.lower()}.")
                del st.session_state.buy
                st.session_state.flash = f"🎉 Order #{oid} placed! Open **My Orders & Tracking** to follow it."
                st.rerun()


def customer(u):
    if "buy" in st.session_state:
        checkout(u)
        return
    unseen = len(q("SELECT 1 FROM notifications WHERE user_id=?", (u["id"],)))
    tabs = st.tabs(["🛍️ Shop", "📦 My Orders & Tracking", f"🔔 Notifications ({unseen})", "📝 Complaints"])
    with tabs[0]:
        s = st.text_input("🔍 Search products", key="search")
        prods = q("SELECT * FROM products WHERE name LIKE ?", (f"%{s.strip()}%",))
        if not prods:
            st.info("No products found.")
        cols = st.columns(3)
        for i, p in enumerate(prods):
            with cols[i % 3]:
                H(f'<div class="pcard"><div class="em">{esc(p["icon"] or "📦")}</div><div class="nm">{esc(p["name"])}</div><div class="pr">Rs {p["price"]:,.0f}</div></div>')
                if st.button("Buy now", key=f"o{p['id']}", type="primary"):
                    st.session_state.buy = dict(p)
                    st.rerun()
    with tabs[1]:
        orders = q(ORDER_SQL + " WHERE o.customer_id=? ORDER BY o.id DESC", (u["id"],))
        if not orders:
            st.info("No orders yet — head to the shop!")
        for o in orders:
            with st.expander(f"{o['picon'] or '📦'} #{o['id']} · {o['pname']} × {o['qty']} · {o['status']} · Rs {o['total']:,.0f}"):
                H(timeline(o))
                if o["status"] == "In box":
                    st.success("Your parcel is ready! Scan this QR at the box screen:")
                    st.image(qr_png(f"PICK:{o['box_id']}:{o['pick_token']}"), width=200)
                    st.code(f"PICK:{o['box_id']}:{o['pick_token']}")
                H(receipt(o))
                if o["status"] in ("Waiting for box", "Placed") and st.button("Cancel order", key=f"c{o['id']}"):
                    cancel_order(o)
                    st.rerun()
                st.markdown("**💬 Chat**")
                chat_box(o["id"], u["name"])
    with tabs[2]:
        notes = q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (u["id"],))
        if not notes:
            st.info("No notifications yet.")
        for n in notes:
            H(f'<div class="note">{esc(n["msg"])}<br><small>{n["ts"]}</small></div>')
    with tabs[3]:
        t = st.text_area("Describe your issue", key="comp")
        if st.button("Submit complaint", type="primary"):
            if t.strip():
                q("INSERT INTO complaints(customer_id,text,ts) VALUES(?,?,?)", (u["id"], t.strip(), now()), commit=True)
                st.success("Sent to admin.")
            else:
                st.warning("Please write your complaint first.")
        for c in q("SELECT * FROM complaints WHERE customer_id=? ORDER BY id DESC", (u["id"],)):
            H(f'<div class="note">{esc(c["text"])}<br>{badge(c["status"])} <small>{c["ts"]}</small></div>')


# ======================= rider =======================
def rider(u):
    tabs = st.tabs(["🆕 Available", "🚚 My Deliveries"])
    with tabs[0]:
        avail = q(ORDER_SQL + " WHERE o.status='Placed' AND o.rider_id IS NULL AND o.pay_status!='Pending verification' ORDER BY o.id")
        if not avail:
            st.info("No orders available right now.")
        for o in avail:
            c = st.columns([5, 1])
            c[0].markdown(f"**#{o['id']}** {o['picon']} {o['pname']} × {o['qty']} · {o['method']}" + (f" · {o['lname']}" if o["lname"] else ""))
            if c[1].button("Claim", key=f"cl{o['id']}", type="primary"):
                q("UPDATE orders SET rider_id=?, status='Out for delivery' WHERE id=? AND status='Placed' AND rider_id IS NULL",
                  (u["id"], o["id"]), commit=True)
                notify(o["customer_id"], f"Order #{o['id']} is out for delivery.")
                st.rerun()
    with tabs[1]:
        mine = q(ORDER_SQL + " WHERE o.rider_id=? AND o.status IN ('Out for delivery','In box') ORDER BY o.id DESC", (u["id"],))
        if not mine:
            st.info("No active deliveries.")
        for o in mine:
            with st.container(border=True):
                H(f"<b>#{o['id']} {esc(o['picon'] or '')} {esc(o['pname'])}</b> {badge(o['status'])} {badge(o['pay_status'])}")
                if o["status"] == "In box":
                    st.success(f"Delivered to box {o['blabel']}. Waiting for customer pickup.")
                elif o["method"] == "Smart Lock":
                    st.write(f"Drop at box **{o['blabel']}** ({o['lname']}). Show this QR to the box screen:")
                    st.image(qr_png(f"DROP:{o['box_id']}:{o['drop_token']}"), width=180)
                    st.code(f"DROP:{o['box_id']}:{o['drop_token']}")
                else:
                    st.write(f"🏠 {o['address']}  ·  📞 {o['phone']}")
                    if o["pay_method"] == "Cash on Delivery":
                        st.warning(f"💵 Collect Rs {o['total']:,.0f} in cash.")
                    if st.button("Mark delivered", key=f"d{o['id']}", type="primary"):
                        q("UPDATE orders SET status='Completed', pay_status='Paid' WHERE id=?", (o["id"],), commit=True)
                        notify(o["customer_id"], f"Order #{o['id']} delivered.")
                        st.rerun()
                chat_box(o["id"], u["name"] + " (Rider)")


# ======================= seller =======================
def seller(u):
    tabs = st.tabs(["🏷️ My Products", "📦 Orders for my products"])
    with tabs[0]:
        prods = q("SELECT * FROM products WHERE seller_id=?", (u["id"],))
        cols = st.columns(4)
        for i, p in enumerate(prods):
            with cols[i % 4]:
                H(f'<div class="pcard"><div class="em">{esc(p["icon"] or "📦")}</div><div class="nm">{esc(p["name"])}</div><div class="pr">Rs {p["price"]:,.0f}</div></div>')
        with st.form("new_product", clear_on_submit=True):
            st.markdown("**Add a product**")
            n = st.text_input("Product name")
            c1, c2 = st.columns(2)
            pr = c1.number_input("Price (Rs)", min_value=1.0, value=1000.0, step=100.0)
            ic = c2.selectbox("Icon", ICONS)
            if st.form_submit_button("Add product", type="primary"):
                if n.strip():
                    q("INSERT INTO products(name,price,seller_id,icon) VALUES(?,?,?,?)", (n.strip(), pr, u["id"], ic), commit=True)
                    st.rerun()
                else:
                    st.warning("Enter a product name.")
    with tabs[1]:
        rows = q("""SELECT o.id, p.name product, o.qty, o.method, o.pay_method, o.pay_status, o.status, o.total, o.created FROM orders o
                    JOIN products p ON p.id=o.product_id WHERE p.seller_id=? ORDER BY o.id DESC""", (u["id"],))
        st.dataframe([dict(r) for r in rows]) if rows else st.info("No orders yet.")


# ======================= admin =======================
def admin(u):
    tabs = st.tabs(["📊 Overview", "📦 Boxes & Reservations", "🔑 Box Scanner (OLED sim)", "🧾 Orders & Payments", "📝 Complaints", "👥 Users"])
    with tabs[0]:
        c = st.columns(5)
        c[0].metric("Orders", q("SELECT COUNT(*) n FROM orders", one=True)["n"])
        c[1].metric("Revenue (paid)", f"Rs {q('SELECT COALESCE(SUM(total),0) n FROM orders WHERE pay_status=? AND status!=?', ('Paid', 'Cancelled'), one=True)['n']:,.0f}")
        c[2].metric("Free boxes", q("SELECT COUNT(*) n FROM boxes WHERE status='free'", one=True)["n"])
        c[3].metric("Open complaints", q("SELECT COUNT(*) n FROM complaints WHERE status='Open'", one=True)["n"])
        c[4].metric("Users", q("SELECT COUNT(*) n FROM users", one=True)["n"])
        rows = q("SELECT status, COUNT(*) n FROM orders GROUP BY status")
        if rows:
            st.markdown("#### Orders by status")
            st.bar_chart(pd.DataFrame({"Orders": [r["n"] for r in rows]}, index=[r["status"] for r in rows]))
    with tabs[1]:
        for b in q("SELECT b.*, l.name lname FROM boxes b JOIN locations l ON l.id=b.location_id ORDER BY b.id"):
            c = st.columns([2, 1.3, 1, 1, 1, 1.4])
            c[0].write(f"**{b['label']}** · {b['lname']}")
            c[1].write(f"{b['status']} · {'🔒' if b['locked'] else '🔓'}")
            if c[2].button("Unlock", key=f"u{b['id']}"):
                q("UPDATE boxes SET locked=0 WHERE id=?", (b["id"],), commit=True)
                st.rerun()
            if c[3].button("Lock", key=f"l{b['id']}"):
                q("UPDATE boxes SET locked=1 WHERE id=?", (b["id"],), commit=True)
                st.rerun()
            if c[4].button("Block", key=f"b{b['id']}"):
                block_box(b["id"])
                st.rerun()
            if c[5].button("Free/Unblock", key=f"f{b['id']}"):
                if q("SELECT 1 FROM orders WHERE box_id=? AND status IN ('Placed','Out for delivery','In box')", (b["id"],), one=True):
                    st.warning("This box is held by an active order. Cancel that order first.")
                else:
                    release_box(b["id"])
                    st.rerun()
        st.divider()
        st.subheader("Allocate boxes to waiting orders")
        waiting = q("SELECT o.*, l.name lname FROM orders o LEFT JOIN locations l ON l.id=o.location_id WHERE o.status='Waiting for box' ORDER BY o.id")
        if not waiting:
            st.caption("No orders are waiting for a box.")
        for o in waiting:
            free = q("SELECT b.*, l.name lname FROM boxes b JOIN locations l ON l.id=b.location_id WHERE b.status='free'")
            if not free:
                st.write(f"Order #{o['id']} (wanted {o['lname']}): no free boxes anywhere.")
                continue
            opts = {f"{b['label']} ({b['lname']})": b for b in free}
            c = st.columns([3, 1])
            pick = c[0].selectbox(f"Order #{o['id']} (wanted {o['lname']})", list(opts), key=f"sel{o['id']}")
            if c[1].button("Allocate", key=f"al{o['id']}"):
                b = opts[pick]
                q("UPDATE boxes SET status='reserved' WHERE id=?", (b["id"],), commit=True)
                q("UPDATE orders SET box_id=?, location_id=?, status='Placed' WHERE id=?", (b["id"], b["location_id"], o["id"]), commit=True)
                notify(o["customer_id"], f"Box {b['label']} ({b['lname']}) allocated to your order #{o['id']}.")
                st.rerun()
    with tabs[2]:
        st.caption("Simulates the box scanner: paste the QR text from a rider (DROP:...) or customer (PICK:...).")
        code = st.text_input("Scanned QR content", placeholder="DROP:1:AB12CD34")
        if st.button("Scan", type="primary"):
            parts = code.strip().upper().split(":")
            if len(parts) != 3 or parts[0] not in ("DROP", "PICK") or not parts[1].isdigit():
                st.error("❌ Unrecognised QR code.")
            else:
                kind, bid, tok = parts[0], int(parts[1]), parts[2]
                col = "drop_token" if kind == "DROP" else "pick_token"
                o = q(f"SELECT * FROM orders WHERE box_id=? AND {col}=?", (bid, tok), one=True)
                b = q("SELECT * FROM boxes WHERE id=?", (bid,), one=True)
                if not o or not b or b["status"] == "blocked":
                    st.error("❌ Access denied.")
                elif kind == "DROP" and o["status"] == "Out for delivery":
                    q("UPDATE boxes SET status='occupied', locked=1 WHERE id=?", (bid,), commit=True)
                    q("UPDATE orders SET status='In box' WHERE id=?", (o["id"],), commit=True)
                    notify(o["customer_id"], f"Order #{o['id']} is in box {b['label']}. Use your pickup QR to collect it.")
                    st.success("🔓 Box opened for rider, parcel stored, box re-locked.")
                elif kind == "PICK" and o["status"] == "In box":
                    q("UPDATE orders SET status='Completed' WHERE id=?", (o["id"],), commit=True)
                    release_box(bid)
                    notify(o["customer_id"], f"Order #{o['id']} picked up. Thank you!")
                    st.success("🔓 Box opened for customer. Order completed.")
                else:
                    st.error("❌ This code is not valid at the current order stage.")
    with tabs[3]:
        orders = q(ORDER_SQL.replace("FROM orders o", ", u.name cname FROM orders o JOIN users u ON u.id=o.customer_id") + " ORDER BY o.id DESC")
        if not orders:
            st.info("No orders yet.")
        for o in orders:
            c = st.columns([6, 1.2, 1])
            c[0].markdown(f"**#{o['id']}** {o['cname']} · {o['pname']} × {o['qty']} · Rs {o['total']:,.0f} · {o['pay_method']}")
            c[0].markdown(f"{badge(o['status'])} {badge(o['pay_status'])}", unsafe_allow_html=True)
            if o["pay_status"] == "Pending verification" and o["status"] != "Cancelled" and c[1].button("Verify payment", key=f"vp{o['id']}", type="primary"):
                q("UPDATE orders SET pay_status='Paid' WHERE id=?", (o["id"],), commit=True)
                notify(o["customer_id"], f"Payment for order #{o['id']} verified.")
                st.rerun()
            if o["status"] in ACTIVE and c[2].button("Cancel", key=f"ac{o['id']}"):
                cancel_order(o)
                notify(o["customer_id"], f"Order #{o['id']} was cancelled by admin.")
                st.rerun()
            if o["pay_status"] == "Refund pending" and c[1].button("Mark refunded", key=f"rf{o['id']}"):
                q("UPDATE orders SET pay_status='Refunded' WHERE id=?", (o["id"],), commit=True)
                notify(o["customer_id"], f"Refund for order #{o['id']} issued.")
                st.rerun()
    with tabs[4]:
        rows = q("SELECT c.*, u.name FROM complaints c JOIN users u ON u.id=c.customer_id ORDER BY c.id DESC")
        if not rows:
            st.info("No complaints.")
        for c in rows:
            cc = st.columns([5, 1])
            cc[0].markdown(f"**{c['name']}**: {c['text']}  \n{badge(c['status'])}", unsafe_allow_html=True)
            if c["status"] == "Open" and cc[1].button("Resolve", key=f"r{c['id']}"):
                q("UPDATE complaints SET status='Resolved' WHERE id=?", (c["id"],), commit=True)
                notify(c["customer_id"], "Your complaint was resolved.")
                st.rerun()
    with tabs[5]:
        st.dataframe([dict(r) for r in q("SELECT id, username, name, role FROM users")])


# ======================= main =======================
H(CSS)
if "user" not in st.session_state:
    login_page()
    footer()
    st.stop()

user = st.session_state.user
with st.sidebar:
    H(f'<div class="note"><b>👤 {esc(user["name"])}</b><br><small>{user["role"].title()} account</small></div>')
    with st.expander("🔑 Change password"):
        with st.form("cpw", clear_on_submit=True):
            old = st.text_input("Current password", type="password")
            new = st.text_input("New password (min 6)", type="password")
            if st.form_submit_button("Update", type="primary"):
                row = q("SELECT pw FROM users WHERE id=?", (user["id"],), one=True)
                if not check_pw(old, row["pw"]):
                    st.error("Current password is wrong.")
                elif len(new) < 6:
                    st.error("New password is too short.")
                else:
                    q("UPDATE users SET pw=? WHERE id=?", (hp(new), user["id"]), commit=True)
                    st.success("Password updated.")
    if st.button("Logout"):
        st.session_state.clear()
        st.rerun()
hero(f"{user['role'].title()} dashboard · welcome back, {esc(user['name'].split()[0])}!")
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
{"customer": customer, "rider": rider, "seller": seller, "admin": admin}[user["role"]](user)
footer()
