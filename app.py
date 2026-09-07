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
BUILD = "v1.0-turso"

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

    conn = libsql.connect(database=url, auth_token=token)
    return conn


def init_schema(conn):
    """Teljes séma létrehozása a React alkalmazás adatmodellje alapján."""
    c = conn.cursor()

    # Termékek
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            sku TEXT,
            name TEXT NOT NULL,
            unit TEXT DEFAULT 'kg',
            kg_per_bag REAL,
            kg_per_pallet REAL,
            location TEXT,
            supplier_id TEXT,
            created_at TEXT
        )
    """)

    # Partnerek / Beszállítók
    c.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id TEXT PRIMARY KEY,
            code TEXT,
            name TEXT NOT NULL,
            address TEXT,
            contact TEXT,
            phone TEXT,
            email TEXT,
            created_at TEXT
        )
    """)

    # Batchek (lokáció + mennyiség)
    c.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            batch_number TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            received_qty REAL,
            location TEXT,
            supplier_id TEXT,
            shipment_number TEXT,
            received_at TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        )
    """)

    # Mozgások
    c.execute("""
        CREATE TABLE IF NOT EXISTS movements (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            batch_id TEXT,
            type TEXT NOT NULL,          -- 'be' vagy 'ki'
            quantity REAL NOT NULL,
            note TEXT,
            date TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        )
    """)

    # Megrendelések
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            shipment_number TEXT,
            partner_id TEXT,
            status TEXT DEFAULT 'rögzített',  -- rögzített | kivezetve
            created_at TEXT,
            dispatched_at TEXT,
            FOREIGN KEY (partner_id) REFERENCES suppliers(id)
        )
    """)

    # Megrendelés tételek
    c.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            batch_number TEXT,
            qty REAL NOT NULL,
            allocated INTEGER DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # Allokációk (melyik batchből mennyi)
    c.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            id TEXT PRIMARY KEY,
            order_item_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            location TEXT,
            qty REAL NOT NULL,
            FOREIGN KEY (order_item_id) REFERENCES order_items(id),
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        )
    """)

    # Napi zárások
    c.execute("""
        CREATE TABLE IF NOT EXISTS closings (
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
        )
    """)

    # Díjszabás (évenként)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            id TEXT PRIMARY KEY,
            year INTEGER UNIQUE NOT NULL,
            storage_per_pallet REAL DEFAULT 0,
            incoming_full_pal REAL DEFAULT 0,
            incoming_mixed_pal REAL DEFAULT 0,
            picking_per_line REAL DEFAULT 0,
            outgoing_per_pal REAL DEFAULT 0,
            outgoing_per_bag REAL DEFAULT 0
        )
    """)

    # Beállítások + biztonság
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()


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
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def execute(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    return cur


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
