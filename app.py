import streamlit as st
import pandas as pd
import libsql
from datetime import datetime, date, timedelta
import hashlib
import json
import io
import uuid
from pathlib import Path

# ============================================================
# BEÁLLÍTÁSOK
# ============================================================
st.set_page_config(
    page_title="Raktárkezelő",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

LOCK_AFTER_HOURS = 3
BUILD = "v1.2-fix"

# ============================================================
# ADATBÁZIS KAPCSOLAT (TURSO)
# ============================================================
@st.cache_resource
def get_connection():
    """Turso kapcsolat – a secrets.toml-ból olvassa az adatokat."""
    try:
        url = st.secrets["turso"]["url"]
        token = st.secrets["turso"]["token"]
    except Exception:
        st.error(
            "❌ Nincs beállítva a Turso kapcsolat.\n\n"
            "Hozd létre a `.streamlit/secrets.toml` fájlt ezzel a tartalommal:\n\n"
            "```toml\n[turso]\nurl = \"libsql://...\"\ntoken = \"...\"\n```"
        )
        st.stop()

    try:
        # Hivatalos libsql minta
        conn = libsql.connect(database=url, auth_token=token)
        return conn
    except TypeError:
        # régebbi / más signature
        conn = libsql.connect(url, auth_token=token)
        return conn
    except Exception as e:
        st.error(f"❌ Turso csatlakozási hiba: {e}")
        st.stop()


def init_schema(conn):
    """Teljes séma létrehozása – FOREIGN KEY nélkül a jobb Turso kompatibilitásért."""
    statements = [
        """CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            sku TEXT,
            name TEXT NOT NULL,
            unit TEXT DEFAULT 'kg',
            kg_per_bag REAL,
            kg_per_pallet REAL,
            location TEXT,
            supplier_id TEXT,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS suppliers (
            id TEXT PRIMARY KEY,
            code TEXT,
            name TEXT NOT NULL,
            address TEXT,
            contact TEXT,
            phone TEXT,
            email TEXT,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            batch_number TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            received_qty REAL,
            location TEXT,
            supplier_id TEXT,
            shipment_number TEXT,
            received_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS movements (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            batch_id TEXT,
            type TEXT NOT NULL,
            quantity REAL NOT NULL,
            note TEXT,
            date TEXT NOT NULL,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            shipment_number TEXT,
            partner_id TEXT,
            status TEXT DEFAULT 'rögzített',
            created_at TEXT,
            dispatched_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS order_items (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            batch_number TEXT,
            qty REAL NOT NULL,
            allocated INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS allocations (
            id TEXT PRIMARY KEY,
            order_item_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            location TEXT,
            qty REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS closings (
            id TEXT PRIMARY KEY,
            date TEXT UNIQUE NOT NULL,
            incoming_kg REAL DEFAULT 0,
            incoming_full_pal REAL DEFAULT 0,
            incoming_mixed_pal REAL DEFAULT 0,
            outgoing_kg REAL DEFAULT 0,
            outgoing_pal REAL DEFAULT 0,
            outgoing_bags REAL DEFAULT 0,
            picking_lines INTEGER DEFAULT 0,
            pal_on_stock REAL DEFAULT 0,
            storage_fee REAL DEFAULT 0,
            incoming_fee REAL DEFAULT 0,
            picking_fee REAL DEFAULT 0,
            outgoing_fee REAL DEFAULT 0,
            closed_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS rates (
            id TEXT PRIMARY KEY,
            year INTEGER UNIQUE NOT NULL,
            storage_per_pallet REAL DEFAULT 0,
            incoming_full_pal REAL DEFAULT 0,
            incoming_mixed_pal REAL DEFAULT 0,
            picking_per_line REAL DEFAULT 0,
            outgoing_per_pal REAL DEFAULT 0,
            outgoing_per_bag REAL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""",
    ]
    for sql in statements:
        try:
            conn.execute(sql)
        except Exception:
            try:
                c = conn.cursor()
                c.execute(sql)
            except Exception:
                pass
    try:
        conn.commit()
    except Exception:
        pass



def uid():
    return str(uuid.uuid4())


def now_iso():
    return datetime.now().isoformat()


def today_str():
    return date.today().isoformat()


# ============================================================
# SEGÉDFÜGGVÉNYEK
# ============================================================
def query_df(conn, sql, params=()):
    try:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if getattr(cur, "description", None) else []
        if not cols and rows:
            cols = [f"c{i}" for i in range(len(rows[0]))]
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        cur = conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)


def execute(conn, sql, params=()):
    try:
        if params:
            conn.execute(sql, params)
        else:
            conn.execute(sql)
        conn.commit()
    except Exception:
        cur = conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        conn.commit()
    return None


def get_setting(conn, key, default=None):
    df = query_df(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    if df.empty:
        return default
    return df.iloc[0]["value"]


def set_setting(conn, key, value):
    execute(conn, """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# KÉSZLET SZÁMÍTÁS
# ============================================================
def total_stock(conn, product_id: str) -> float:
    df = query_df(conn, "SELECT COALESCE(SUM(quantity), 0) AS s FROM batches WHERE product_id = ?", (product_id,))
    return float(df.iloc[0]["s"]) if not df.empty else 0.0


def get_products_with_stock(conn):
    sql = """
        SELECT
            p.id, p.sku, p.name, p.unit, p.kg_per_bag, p.kg_per_pallet,
            p.location, p.supplier_id,
            COALESCE(SUM(b.quantity), 0) AS stock
        FROM products p
        LEFT JOIN batches b ON b.product_id = p.id
        GROUP BY p.id
        ORDER BY p.name
    """
    return query_df(conn, sql)


# ============================================================
# AUTH / ZÁROLÁS
# ============================================================
def check_auth(conn):
    """Visszaadja az auth állapotot és kezeli a zárolást."""
    if "auth_state" not in st.session_state:
        st.session_state.auth_state = "checking"
    if "last_activity" not in st.session_state:
        st.session_state.last_activity = datetime.now()

    # Aktivitás frissítése
    st.session_state.last_activity = datetime.now()

    pw_hash = get_setting(conn, "password_hash")

    if not pw_hash:
        return "unlocked"  # Nincs jelszó beállítva

    # Ha van jelszó és lejárt az idő
    if st.session_state.auth_state == "unlocked":
        elapsed = (datetime.now() - st.session_state.last_activity).total_seconds()
        if elapsed > LOCK_AFTER_HOURS * 3600:
            st.session_state.auth_state = "locked"

    return st.session_state.auth_state


def login_screen(conn):
    st.title("🔒 Raktárkezelő – Belépés")
    st.caption(BUILD)

    pw = st.text_input("Jelszó", type="password", key="login_pw")
    if st.button("Belépés", type="primary"):
        stored = get_setting(conn, "password_hash")
        if stored and sha256(pw) == stored:
            st.session_state.auth_state = "unlocked"
            st.session_state.last_activity = datetime.now()
            st.rerun()
        else:
            st.error("Hibás jelszó.")


def setup_password_screen(conn):
    st.title("🔐 Közös jelszó beállítása")
    st.info("Ezt a jelszót mindenki használni fogja. 3 óra tétlenség után újra be kell írni.")

    pw1 = st.text_input("Új jelszó", type="password")
    pw2 = st.text_input("Jelszó megerősítése", type="password")

    if st.button("Jelszó beállítása és belépés", type="primary"):
        if len(pw1) < 4:
            st.error("Legalább 4 karakter legyen.")
        elif pw1 != pw2:
            st.error("A két jelszó nem egyezik.")
        else:
            set_setting(conn, "password_hash", sha256(pw1))
            st.session_state.auth_state = "unlocked"
            st.success("Jelszó elmentve.")
            st.rerun()


# ============================================================
# OLDALAK
# ============================================================
def page_home(conn):
    st.header("📦 Áttekintés")

    products = get_products_with_stock(conn)
    batches = query_df(conn, "SELECT * FROM batches WHERE quantity > 0")
    pending = query_df(conn, "SELECT COUNT(*) AS c FROM orders WHERE status = 'rögzített'")
    today_mov = query_df(conn, "SELECT COUNT(*) AS c FROM movements WHERE date(date) = date('now')")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Termékek készleten", len(products[products["stock"] > 0]) if not products.empty else 0)
    c2.metric("Aktív batchek", len(batches))
    c3.metric("Függő megrendelések", int(pending.iloc[0]["c"]) if not pending.empty else 0)
    c4.metric("Mai mozgások", int(today_mov.iloc[0]["c"]) if not today_mov.empty else 0)

    st.subheader("Alacsony / elfogyott készlet")
    if not products.empty:
        low = products[products["stock"] <= 0]
        if not low.empty:
            st.dataframe(low[["sku", "name", "stock", "unit"]], use_container_width=True, hide_index=True)
        else:
            st.success("Nincs elfogyott termék.")
    else:
        st.info("Még nincsenek termékek.")


def page_products(conn):
    st.header("Termékek")

    tab1, tab2 = st.tabs(["Lista", "Új termék"])

    with tab1:
        df = get_products_with_stock(conn)
        if df.empty:
            st.info("Még nincsenek termékek.")
        else:
            st.dataframe(
                df[["sku", "name", "stock", "unit", "kg_per_bag", "kg_per_pallet"]],
                use_container_width=True,
                hide_index=True
            )

            # Törlés
            with st.expander("Termék törlése"):
                options = {f"{r['name']} ({r['sku'] or '-'})": r["id"] for _, r in df.iterrows()}
                sel = st.selectbox("Válassz terméket", list(options.keys()))
                if st.button("Törlés", type="primary"):
                    pid = options[sel]
                    # Csak akkor engedjük, ha nincs batch
                    b = query_df(conn, "SELECT COUNT(*) AS c FROM batches WHERE product_id = ? AND quantity > 0", (pid,))
                    if int(b.iloc[0]["c"]) > 0:
                        st.error("Van még készlet ezen a terméken – előbb ürítsd ki.")
                    else:
                        execute(conn, "DELETE FROM products WHERE id = ?", (pid,))
                        st.success("Törölve.")
                        st.rerun()

    with tab2:
        with st.form("new_product"):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("Termék név *")
                sku = st.text_input("Cikkszám / SKU")
                unit = st.selectbox("Egység", ["kg", "db", "doboz", "raklap", "liter", "méter", "csomag"])
            with col2:
                kg_bag = st.number_input("Kg / zsák", min_value=0.0, value=25.0, step=1.0)
                kg_pal = st.number_input("Kg / raklap", min_value=0.0, value=1000.0, step=50.0)
                location = st.text_input("Alapértelmezett hely")

            if st.form_submit_button("Hozzáadás"):
                if not name.strip():
                    st.error("A név kötelező.")
                else:
                    execute(conn, """
                        INSERT INTO products (id, sku, name, unit, kg_per_bag, kg_per_pallet, location, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (uid(), sku.strip() or None, name.strip(), unit, kg_bag, kg_pal, location.strip() or None, now_iso()))
                    st.success("Termék hozzáadva.")
                    st.rerun()


def page_suppliers(conn):
    st.header("Partnerek")

    tab1, tab2 = st.tabs(["Lista", "Új partner"])

    with tab1:
        df = query_df(conn, "SELECT * FROM suppliers ORDER BY name")
        if df.empty:
            st.info("Még nincsenek partnerek.")
        else:
            st.dataframe(df[["code", "name", "contact", "phone", "email"]], use_container_width=True, hide_index=True)

    with tab2:
        with st.form("new_supplier"):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("Cégnév *")
                code = st.text_input("Partner kód")
                contact = st.text_input("Kapcsolattartó")
            with col2:
                phone = st.text_input("Telefon")
                email = st.text_input("E-mail")
                address = st.text_area("Cím")

            if st.form_submit_button("Hozzáadás"):
                if not name.strip():
                    st.error("A cégnév kötelező.")
                else:
                    execute(conn, """
                        INSERT INTO suppliers (id, code, name, address, contact, phone, email, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (uid(), code.strip() or None, name.strip(), address.strip() or None,
                          contact.strip() or None, phone.strip() or None, email.strip() or None, now_iso()))
                    st.success("Partner hozzáadva.")
                    st.rerun()


def page_incoming(conn):
    st.header("📥 Bevételezés")

    products = query_df(conn, "SELECT id, name, sku, unit FROM products ORDER BY name")
    suppliers = query_df(conn, "SELECT id, name FROM suppliers ORDER BY name")

    if products.empty:
        st.warning("Először adj hozzá termékeket.")
        return

    with st.form("incoming_form"):
        col1, col2 = st.columns(2)
        with col1:
            prod_opts = {f"{r['name']} ({r['sku'] or '-'})": r["id"] for _, r in products.iterrows()}
            product_sel = st.selectbox("Termék *", list(prod_opts.keys()))
            batch_number = st.text_input("Batch szám *")
            quantity = st.number_input("Mennyiség *", min_value=0.01, value=1000.0, step=25.0)
            location = st.text_input("Lokáció (pl. 01.02.015)")
        with col2:
            supp_opts = {"— nincs —": None}
            if not suppliers.empty:
                supp_opts.update({r["name"]: r["id"] for _, r in suppliers.iterrows()})
            supplier_sel = st.selectbox("Partner", list(supp_opts.keys()))
            shipment = st.text_input("Szállítmányszám")
            received_at = st.date_input("Beérkezés dátuma", value=date.today())
            note = st.text_area("Megjegyzés")

        if st.form_submit_button("Bevételezés rögzítése", type="primary"):
            if not batch_number.strip():
                st.error("A batch szám kötelező.")
            else:
                pid = prod_opts[product_sel]
                sid = supp_opts[supplier_sel]
                batch_id = uid()
                mov_id = uid()
                ts = datetime.combine(received_at, datetime.min.time()).isoformat()

                execute(conn, """
                    INSERT INTO batches (id, product_id, batch_number, quantity, received_qty, location, supplier_id, shipment_number, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (batch_id, pid, batch_number.strip(), quantity, quantity, location.strip() or None, sid, shipment.strip() or None, ts))

                execute(conn, """
                    INSERT INTO movements (id, product_id, batch_id, type, quantity, note, date, created_at)
                    VALUES (?, ?, ?, 'be', ?, ?, ?, ?)
                """, (mov_id, pid, batch_id, quantity, note.strip() or f"szállítmány: {shipment}", ts, now_iso()))

                st.success("Bevételezés rögzítve.")
                st.rerun()


def page_stock(conn):
    st.header("📦 Készlet (batchek)")

    df = query_df(conn, """
        SELECT b.batch_number, p.name AS product, p.sku, b.location, b.quantity, p.unit,
               b.shipment_number, b.received_at, s.name AS supplier
        FROM batches b
        JOIN products p ON p.id = b.product_id
        LEFT JOIN suppliers s ON s.id = b.supplier_id
        WHERE b.quantity > 0
        ORDER BY b.received_at DESC
    """)

    if df.empty:
        st.info("Nincs készleten lévő batch.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Összesítés termékenként
        st.subheader("Összesítés termékenként")
        summary = get_products_with_stock(conn)
        summary = summary[summary["stock"] > 0][["sku", "name", "stock", "unit"]]
        st.dataframe(summary, use_container_width=True, hide_index=True)


def page_movements(conn):
    st.header("📊 Mozgások napló")

    df = query_df(conn, """
        SELECT m.date, m.type, p.name AS product, p.sku, m.quantity, p.unit, m.note, b.batch_number
        FROM movements m
        JOIN products p ON p.id = m.product_id
        LEFT JOIN batches b ON b.id = m.batch_id
        ORDER BY m.date DESC
        LIMIT 500
    """)

    if df.empty:
        st.info("Még nincsenek mozgások.")
    else:
        df["type"] = df["type"].map({"be": "📥 Bevétel", "ki": "📤 Kiadás"})
        st.dataframe(df, use_container_width=True, hide_index=True)


def page_orders(conn):
    st.header("📋 Megrendelések")

    tab_list, tab_new, tab_dispatch = st.tabs(["Lista", "Új megrendelés", "Kivezetés"])

    # ---------- LISTA ----------
    with tab_list:
        orders = query_df(conn, """
            SELECT o.id, o.shipment_number, o.status, o.created_at, o.dispatched_at,
                   s.name AS partner
            FROM orders o
            LEFT JOIN suppliers s ON s.id = o.partner_id
            ORDER BY o.created_at DESC
            LIMIT 200
        """)
        if orders.empty:
            st.info("Még nincsenek megrendelések.")
        else:
            st.dataframe(orders[["shipment_number", "partner", "status", "created_at", "dispatched_at"]],
                         use_container_width=True, hide_index=True)

            # Részletek
            with st.expander("Megrendelés részletei"):
                opts = {f"{r['shipment_number'] or r['id'][:8]} – {r['partner'] or '?'} ({r['status']})": r["id"]
                        for _, r in orders.iterrows()}
                sel = st.selectbox("Válassz megrendelést", list(opts.keys()), key="ord_detail")
                if sel:
                    oid = opts[sel]
                    items = query_df(conn, """
                        SELECT oi.id, p.name, p.sku, oi.batch_number, oi.qty,
                               COALESCE(SUM(a.qty), 0) AS allocated
                        FROM order_items oi
                        JOIN products p ON p.id = oi.product_id
                        LEFT JOIN allocations a ON a.order_item_id = oi.id
                        WHERE oi.order_id = ?
                        GROUP BY oi.id
                    """, (oid,))
                    st.dataframe(items, use_container_width=True, hide_index=True)

                    allocs = query_df(conn, """
                        SELECT p.name, b.batch_number, b.location, a.qty
                        FROM allocations a
                        JOIN order_items oi ON oi.id = a.order_item_id
                        JOIN batches b ON b.id = a.batch_id
                        JOIN products p ON p.id = oi.product_id
                        WHERE oi.order_id = ?
                    """, (oid,))
                    if not allocs.empty:
                        st.markdown("**Allokációk (lokációk):**")
                        st.dataframe(allocs, use_container_width=True, hide_index=True)

    # ---------- ÚJ MEGBRENDELÉS ----------
    with tab_new:
        products = query_df(conn, "SELECT id, name, sku, unit FROM products ORDER BY name")
        suppliers = query_df(conn, "SELECT id, name FROM suppliers ORDER BY name")
        batches = query_df(conn, """
            SELECT b.id, b.batch_number, b.quantity, b.location, p.name AS product, p.id AS product_id
            FROM batches b
            JOIN products p ON p.id = b.product_id
            WHERE b.quantity > 0
            ORDER BY b.batch_number
        """)

        if products.empty:
            st.warning("Nincsenek termékek.")
        else:
            with st.form("new_order"):
                col1, col2 = st.columns(2)
                with col1:
                    shipment = st.text_input("Szállítmányszám")
                    if not suppliers.empty:
                        supp_opts = {r["name"]: r["id"] for _, r in suppliers.iterrows()}
                        partner = st.selectbox("Partner", list(supp_opts.keys()))
                    else:
                        partner = None
                        st.info("Nincs partner – előbb adj hozzá.")
                with col2:
                    st.write("")  # spacer

                st.markdown("**Tételek** (batch szám + mennyiség)")
                # Egyszerű: maximum 5 tétel a formban
                items_data = []
                for i in range(5):
                    c1, c2, c3 = st.columns([3, 2, 2])
                    with c1:
                        bn = st.text_input(f"Batch szám #{i+1}", key=f"bn_{i}")
                    with c2:
                        qty = st.number_input(f"Mennyiség #{i+1}", min_value=0.0, value=0.0, step=25.0, key=f"qty_{i}")
                    with c3:
                        st.write("")
                    if bn.strip() and qty > 0:
                        items_data.append({"batch_number": bn.strip(), "qty": qty})

                submitted = st.form_submit_button("Megrendelés rögzítése", type="primary")
                if submitted:
                    if not items_data:
                        st.error("Legalább egy tételt meg kell adni.")
                    else:
                        order_id = uid()
                        partner_id = supp_opts.get(partner) if partner else None
                        execute(conn, """
                            INSERT INTO orders (id, shipment_number, partner_id, status, created_at)
                            VALUES (?, ?, ?, 'rögzített', ?)
                        """, (order_id, shipment.strip() or None, partner_id, now_iso()))

                        errors = []
                        for it in items_data:
                            # Keressük a batcheket ezzel a batch számmal
                            matching = batches[batches["batch_number"].str.lower() == it["batch_number"].lower()]
                            if matching.empty:
                                errors.append(f"Batch nem található: {it['batch_number']}")
                                continue

                            # Best-fit: a legközelebbi szabad mennyiségű lokáció(k)
                            needed = it["qty"]
                            product_id = matching.iloc[0]["product_id"]
                            item_id = uid()
                            execute(conn, """
                                INSERT INTO order_items (id, order_id, product_id, batch_number, qty, allocated)
                                VALUES (?, ?, ?, ?, ?, 0)
                            """, (item_id, order_id, product_id, it["batch_number"], needed))

                            # Lefoglalt mennyiség (más rögzített rendelésekből)
                            reserved = query_df(conn, """
                                SELECT a.batch_id, COALESCE(SUM(a.qty), 0) AS reserved
                                FROM allocations a
                                JOIN order_items oi ON oi.id = a.order_item_id
                                JOIN orders o ON o.id = oi.order_id
                                WHERE o.status = 'rögzített' AND o.id != ?
                                GROUP BY a.batch_id
                            """, (order_id,))
                            reserved_map = {r["batch_id"]: r["reserved"] for _, r in reserved.iterrows()} if not reserved.empty else {}

                            candidates = []
                            for _, b in matching.iterrows():
                                free = b["quantity"] - reserved_map.get(b["id"], 0)
                                if free > 0.0001:
                                    candidates.append({"id": b["id"], "location": b["location"] or "", "free": free})

                            # Best-fit allokáció
                            remaining = needed
                            candidates = sorted(candidates, key=lambda x: abs(x["free"] - remaining))
                            for c in candidates:
                                if remaining <= 0:
                                    break
                                take = min(c["free"], remaining)
                                execute(conn, """
                                    INSERT INTO allocations (id, order_item_id, batch_id, location, qty)
                                    VALUES (?, ?, ?, ?, ?)
                                """, (uid(), item_id, c["id"], c["location"], take))
                                remaining -= take

                            if remaining > 0.0001:
                                errors.append(f"Nem volt elég szabad készlet a batchhez: {it['batch_number']} (hiány: {remaining:.1f})")
                            else:
                                execute(conn, "UPDATE order_items SET allocated = 1 WHERE id = ?", (item_id,))

                        if errors:
                            st.warning("Megrendelés rögzítve, de voltak problémák:\n" + "\n".join(errors))
                        else:
                            st.success("Megrendelés sikeresen rögzítve és allokálva.")
                        st.rerun()

    # ---------- KIVEZETÉS ----------
    with tab_dispatch:
        pending = query_df(conn, """
            SELECT o.id, o.shipment_number, s.name AS partner, o.created_at
            FROM orders o
            LEFT JOIN suppliers s ON s.id = o.partner_id
            WHERE o.status = 'rögzített'
            ORDER BY o.created_at
        """)
        if pending.empty:
            st.info("Nincs kivezetésre váró megrendelés.")
        else:
            opts = {f"{r['shipment_number'] or r['id'][:8]} – {r['partner'] or '?'}": r["id"]
                    for _, r in pending.iterrows()}
            sel = st.selectbox("Megrendelés kivezetése", list(opts.keys()))
            if st.button("Kivezetés végrehajtása", type="primary"):
                oid = opts[sel]
                # Allokációk alapján csökkentjük a batcheket és létrehozunk ki mozgásokat
                allocs = query_df(conn, """
                    SELECT a.batch_id, a.qty, a.location, oi.product_id, b.batch_number
                    FROM allocations a
                    JOIN order_items oi ON oi.id = a.order_item_id
                    JOIN batches b ON b.id = a.batch_id
                    WHERE oi.order_id = ?
                """, (oid,))

                for _, a in allocs.iterrows():
                    # Batch mennyiség csökkentése
                    execute(conn, "UPDATE batches SET quantity = quantity - ? WHERE id = ?", (a["qty"], a["batch_id"]))
                    # Ki mozgás
                    execute(conn, """
                        INSERT INTO movements (id, product_id, batch_id, type, quantity, note, date, created_at)
                        VALUES (?, ?, ?, 'ki', ?, ?, ?, ?)
                    """, (uid(), a["product_id"], a["batch_id"], a["qty"],
                          f"Kivezetés – szállítmány, batch: {a['batch_number']}, hely: {a['location']}",
                          now_iso(), now_iso()))

                execute(conn, "UPDATE orders SET status = 'kivezetve', dispatched_at = ? WHERE id = ?",
                        (now_iso(), oid))
                st.success("Kivezetés kész. A készlet frissült.")
                st.rerun()


def page_settings(conn):
    st.header("⚙️ Beállítások")

    st.subheader("Jelszó")
    current_hash = get_setting(conn, "password_hash")
    if current_hash:
        st.success("Jelszavas védelem be van kapcsolva.")
        if st.button("Jelszó törlése (csak ha tudod a jelenlegit)"):
            st.session_state.show_remove_pw = True
    else:
        st.info("Jelenleg nincs jelszó beállítva.")
        if st.button("Jelszó beállítása"):
            st.session_state.auth_state = "setup"
            st.rerun()

    st.markdown("---")
    st.subheader("Adatbázis info")
    st.code(f"Build: {BUILD}\nTurso kapcsolat: aktív")

    # Egyszerű export
    st.subheader("Gyors export")
    if st.button("Termékek + Készlet exportálása Excelbe"):
        products = get_products_with_stock(conn)
        batches = query_df(conn, """
            SELECT b.batch_number, p.name, p.sku, b.location, b.quantity, p.unit, b.received_at
            FROM batches b JOIN products p ON p.id = b.product_id
            WHERE b.quantity > 0
        """)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            products.to_excel(writer, sheet_name="Termékek", index=False)
            batches.to_excel(writer, sheet_name="Készlet", index=False)
        buffer.seek(0)
        st.download_button(
            "Letöltés",
            data=buffer,
            file_name=f"raktar-export-{today_str()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


# ============================================================
# FŐALKALMAZÁS
# ============================================================
def main():
    conn = get_connection()
    init_schema(conn)

    # Auth ellenőrzés
    auth = check_auth(conn)

    if auth == "setup" or (get_setting(conn, "password_hash") is None and st.session_state.get("force_setup")):
        setup_password_screen(conn)
        return

    if auth == "locked":
        login_screen(conn)
        return

    # === FŐMENÜ ===
    st.sidebar.title("📦 Raktárkezelő")
    st.sidebar.caption(BUILD)

    menu = st.sidebar.radio(
        "Menü",
        [
            "🏠 Áttekintés",
            "📥 Bevételezés",
            "📋 Megrendelések",
            "📦 Készlet",
            "📋 Termékek",
            "🚚 Partnerek",
            "📊 Mozgások",
            "⚙️ Beállítások",
        ]
    )

    st.sidebar.markdown("---")
    if st.sidebar.button("Kijelentkezés / Zárolás"):
        st.session_state.auth_state = "locked"
        st.rerun()

    # Oldalak
    if menu == "🏠 Áttekintés":
        page_home(conn)
    elif menu == "📥 Bevételezés":
        page_incoming(conn)
    elif menu == "📋 Megrendelések":
        page_orders(conn)
    elif menu == "📦 Készlet":
        page_stock(conn)
    elif menu == "📋 Termékek":
        page_products(conn)
    elif menu == "🚚 Partnerek":
        page_suppliers(conn)
    elif menu == "📊 Mozgások":
        page_movements(conn)
    elif menu == "⚙️ Beállítások":
        page_settings(conn)


if __name__ == "__main__":
    main()
