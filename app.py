import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime, date
import io
from pathlib import Path

# ==================== BEÁLLÍTÁSOK ====================
DB_PATH = Path(__file__).parent / "raktar.db"
st.set_page_config(
    page_title="Raktárkészlet Kezelő",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== ADATBÁZIS ====================
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    # Termékek tábla
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE,
            name TEXT NOT NULL,
            unit TEXT DEFAULT 'db',
            min_stock REAL DEFAULT 0,
            note TEXT,
            created_at TEXT
        )
    """)
    
    # Készletmozgások tábla
    c.execute("""
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,  -- 'bevetel' vagy 'kiadas'
            quantity REAL NOT NULL,
            movement_date TEXT NOT NULL,
            note TEXT,
            created_at TEXT,
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    """)
    
    conn.commit()
    conn.close()

def get_products_with_stock():
    """Termékek aktuális készlettel"""
    conn = get_connection()
    query = """
        SELECT 
            p.id,
            p.sku,
            p.name,
            p.unit,
            p.min_stock,
            p.note,
            COALESCE(SUM(
                CASE 
                    WHEN m.movement_type = 'bevetel' THEN m.quantity
                    WHEN m.movement_type = 'kiadas' THEN -m.quantity
                    ELSE 0
                END
            ), 0) as stock
        FROM products p
        LEFT JOIN movements m ON p.id = m.product_id
        GROUP BY p.id
        ORDER BY p.name
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def get_movements(limit=500):
    conn = get_connection()
    query = """
        SELECT 
            m.id,
            m.movement_date,
            p.sku,
            p.name as product_name,
            m.movement_type,
            m.quantity,
            p.unit,
            m.note,
            m.created_at
        FROM movements m
        JOIN products p ON m.product_id = p.id
        ORDER BY m.movement_date DESC, m.id DESC
        LIMIT ?
    """
    df = pd.read_sql_query(query, conn, params=(limit,))
    conn.close()
    return df

def add_product(sku, name, unit, min_stock, note):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO products (sku, name, unit, min_stock, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sku or None, name, unit, min_stock, note, datetime.now().isoformat())
        )
        conn.commit()
        return True, "Termék sikeresen hozzáadva!"
    except sqlite3.IntegrityError:
        return False, "Ez a SKU már létezik!"
    except Exception as e:
        return False, f"Hiba: {e}"
    finally:
        conn.close()

def update_product(product_id, sku, name, unit, min_stock, note):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE products SET sku=?, name=?, unit=?, min_stock=?, note=? WHERE id=?",
            (sku or None, name, unit, min_stock, note, product_id)
        )
        conn.commit()
        return True, "Termék frissítve!"
    except sqlite3.IntegrityError:
        return False, "Ez a SKU már létezik!"
    except Exception as e:
        return False, f"Hiba: {e}"
    finally:
        conn.close()

def delete_product(product_id):
    conn = get_connection()
    try:
        c = conn.cursor()
        # Először a mozgásokat töröljük
        c.execute("DELETE FROM movements WHERE product_id=?", (product_id,))
        c.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        return True, "Termék törölve!"
    except Exception as e:
        return False, f"Hiba: {e}"
    finally:
        conn.close()

def add_movement(product_id, movement_type, quantity, movement_date, note):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO movements (product_id, movement_type, quantity, movement_date, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (product_id, movement_type, quantity, movement_date, note, datetime.now().isoformat())
        )
        conn.commit()
        return True, "Készletmozgás rögzítve!"
    except Exception as e:
        return False, f"Hiba: {e}"
    finally:
        conn.close()

def delete_movement(movement_id):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM movements WHERE id=?", (movement_id,))
        conn.commit()
        return True, "Mozgás törölve!"
    except Exception as e:
        return False, f"Hiba: {e}"
    finally:
        conn.close()

# ==================== SEGÉDFÜGGVÉNYEK ====================
def style_stock(val, min_stock):
    if val <= 0:
        return "background-color: #ffcccc; color: #990000; font-weight: bold"
    elif val <= min_stock:
        return "background-color: #fff3cd; color: #856404"
    return ""

# ==================== ALKALMAZÁS ====================
def main():
    init_db()
    
    st.title("📦 Raktárkészlet Kezelő")
    st.caption("Egyszerű, ingyenes készletkezelő rendszer")
    
    # Oldalsáv navigáció
    menu = st.sidebar.radio(
        "Menü",
        ["🏠 Kezdőlap", "📋 Termékek", "📥 Bevételezés / Kiadás", "📊 Mozgások napló", "📁 Import / Export"]
    )
    
    st.sidebar.markdown("---")
    st.sidebar.info("Max. 5 felhasználóval ajánlott. Az adatok a szerveren tárolódnak.")
    
    # ========== KEZDŐLAP ==========
    if menu == "🏠 Kezdőlap":
        st.header("Áttekintés")
        
        df = get_products_with_stock()
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Termékek száma", len(df))
        with col2:
            low = len(df[df["stock"] <= df["min_stock"]]) if not df.empty else 0
            st.metric("Alacsony készlet", low, delta_color="inverse")
        with col3:
            zero = len(df[df["stock"] <= 0]) if not df.empty else 0
            st.metric("Elfogyott", zero, delta_color="inverse")
        with col4:
            total_qty = df["stock"].sum() if not df.empty else 0
            st.metric("Összes darab", f"{total_qty:.0f}")
        
        st.subheader("Alacsony készletű termékek")
        if not df.empty:
            low_df = df[df["stock"] <= df["min_stock"]].copy()
            if not low_df.empty:
                low_df = low_df[["sku", "name", "stock", "min_stock", "unit"]]
                low_df.columns = ["SKU", "Név", "Készlet", "Min. készlet", "Egység"]
                st.dataframe(low_df, use_container_width=True, hide_index=True)
            else:
                st.success("Nincs alacsony készletű termék. 👍")
        else:
            st.info("Még nincsenek termékek. Menj a **Termékek** menüpontra!")
    
    # ========== TERMÉKEK ==========
    elif menu == "📋 Termékek":
        st.header("Termékek")
        
        tab1, tab2 = st.tabs(["Lista", "Új termék / Szerkesztés"])
        
        with tab1:
            df = get_products_with_stock()
            if df.empty:
                st.info("Még nincsenek termékek.")
            else:
                display_df = df[["id", "sku", "name", "stock", "min_stock", "unit", "note"]].copy()
                display_df.columns = ["ID", "SKU", "Név", "Készlet", "Min. készlet", "Egység", "Megjegyzés"]
                
                # Színezés
                def highlight(row):
                    styles = [""] * len(row)
                    stock = row["Készlet"]
                    min_s = row["Min. készlet"]
                    if stock <= 0:
                        styles[3] = "background-color: #ffcccc; color: #990000; font-weight: bold"
                    elif stock <= min_s:
                        styles[3] = "background-color: #fff3cd; color: #856404"
                    return styles
                
                st.dataframe(
                    display_df.style.apply(highlight, axis=1),
                    use_container_width=True,
                    hide_index=True
                )
                
                # Törlés
                st.subheader("Termék törlése")
                del_id = st.number_input("Törlendő termék ID", min_value=1, step=1, key="del_prod")
                if st.button("Törlés", type="primary"):
                    ok, msg = delete_product(int(del_id))
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        
        with tab2:
            st.subheader("Új termék hozzáadása")
            with st.form("add_product_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    sku = st.text_input("SKU / Cikkszám (opcionális)")
                    name = st.text_input("Termék neve *", placeholder="pl. Csavar M8")
                    unit = st.selectbox("Egység", ["db", "kg", "m", "l", "csomag", "doboz", "pár"])
                with col2:
                    min_stock = st.number_input("Minimum készlet", min_value=0.0, value=0.0, step=1.0)
                    note = st.text_area("Megjegyzés")
                
                submitted = st.form_submit_button("Hozzáadás")
                if submitted:
                    if not name.strip():
                        st.error("A termék neve kötelező!")
                    else:
                        ok, msg = add_product(sku.strip() or None, name.strip(), unit, min_stock, note.strip())
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            
            st.markdown("---")
            st.subheader("Meglévő termék szerkesztése")
            df = get_products_with_stock()
            if not df.empty:
                options = {f"{row['name']} (ID: {row['id']})": row['id'] for _, row in df.iterrows()}
                selected = st.selectbox("Válassz terméket", list(options.keys()))
                if selected:
                    pid = options[selected]
                    prod = df[df["id"] == pid].iloc[0]
                    
                    with st.form("edit_product_form"):
                        col1, col2 = st.columns(2)
                        with col1:
                            sku_e = st.text_input("SKU", value=prod["sku"] or "")
                            name_e = st.text_input("Név *", value=prod["name"])
                            unit_e = st.selectbox("Egység", ["db", "kg", "m", "l", "csomag", "doboz", "pár"], 
                                                  index=["db", "kg", "m", "l", "csomag", "doboz", "pár"].index(prod["unit"]) if prod["unit"] in ["db", "kg", "m", "l", "csomag", "doboz", "pár"] else 0)
                        with col2:
                            min_e = st.number_input("Minimum készlet", min_value=0.0, value=float(prod["min_stock"]), step=1.0)
                            note_e = st.text_area("Megjegyzés", value=prod["note"] or "")
                        
                        if st.form_submit_button("Mentés"):
                            ok, msg = update_product(pid, sku_e.strip() or None, name_e.strip(), unit_e, min_e, note_e.strip())
                            if ok:
                                st.success(msg)
                                st.rerun()
                            else:
                                st.error(msg)
    
    # ========== BEVÉTELEZÉS / KIADÁS ==========
    elif menu == "📥 Bevételezés / Kiadás":
        st.header("Bevételezés / Kiadás")
        
        df = get_products_with_stock()
        if df.empty:
            st.warning("Először adj hozzá termékeket a **Termékek** menüben!")
        else:
            options = {f"{row['name']} (készlet: {row['stock']:.0f} {row['unit']})": row['id'] for _, row in df.iterrows()}
            
            with st.form("movement_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    selected = st.selectbox("Termék *", list(options.keys()))
                    movement_type = st.radio("Típus *", ["bevetel", "kiadas"], 
                                            format_func=lambda x: "📥 Bevételezés" if x == "bevetel" else "📤 Kiadás",
                                            horizontal=True)
                    quantity = st.number_input("Mennyiség *", min_value=0.01, value=1.0, step=1.0)
                with col2:
                    movement_date = st.date_input("Dátum *", value=date.today())
                    note = st.text_area("Megjegyzés (pl. szállító, cél)")
                
                submitted = st.form_submit_button("Rögzítés", type="primary")
                if submitted:
                    pid = options[selected]
                    ok, msg = add_movement(pid, movement_type, quantity, movement_date.isoformat(), note.strip())
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
    
    # ========== MOZGÁSOK NAPLÓ ==========
    elif menu == "📊 Mozgások napló":
        st.header("Készletmozgások naplója")
        
        df_mov = get_movements()
        if df_mov.empty:
            st.info("Még nincsenek mozgások.")
        else:
            display = df_mov.copy()
            display["movement_type"] = display["movement_type"].map({"bevetel": "📥 Bevételezés", "kiadas": "📤 Kiadás"})
            display = display[["id", "movement_date", "sku", "product_name", "movement_type", "quantity", "unit", "note"]]
            display.columns = ["ID", "Dátum", "SKU", "Termék", "Típus", "Mennyiség", "Egység", "Megjegyzés"]
            
            st.dataframe(display, use_container_width=True, hide_index=True)
            
            # Nyomtatás tipp
            st.info("💡 Nyomtatáshoz használd a böngésző nyomtatás funkcióját (Ctrl+P), vagy exportáld Excelbe.")
            
            # Törlés
            st.subheader("Mozgás törlése")
            del_mov = st.number_input("Törlendő mozgás ID", min_value=1, step=1, key="del_mov")
            if st.button("Mozgás törlése"):
                ok, msg = delete_movement(int(del_mov))
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
    
    # ========== IMPORT / EXPORT ==========
    elif menu == "📁 Import / Export":
        st.header("Import / Export")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📤 Export Excelbe")
            
            # Termékek export
            df_prod = get_products_with_stock()
            if not df_prod.empty:
                export_prod = df_prod[["sku", "name", "stock", "min_stock", "unit", "note"]].copy()
                export_prod.columns = ["SKU", "Név", "Készlet", "Min. készlet", "Egység", "Megjegyzés"]
                
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                    export_prod.to_excel(writer, sheet_name="Termékek", index=False)
                    
                    # Mozgások is
                    df_mov = get_movements(limit=5000)
                    if not df_mov.empty:
                        export_mov = df_mov[["movement_date", "sku", "product_name", "movement_type", "quantity", "unit", "note"]].copy()
                        export_mov["movement_type"] = export_mov["movement_type"].map({"bevetel": "Bevételezés", "kiadas": "Kiadás"})
                        export_mov.columns = ["Dátum", "SKU", "Termék", "Típus", "Mennyiség", "Egység", "Megjegyzés"]
                        export_mov.to_excel(writer, sheet_name="Mozgások", index=False)
                
                buffer.seek(0)
                st.download_button(
                    label="Letöltés Excel (Termékek + Mozgások)",
                    data=buffer,
                    file_name=f"raktar_export_{date.today().isoformat()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.info("Nincs exportálható adat.")
        
        with col2:
            st.subheader("📥 Import Excelből")
            st.markdown("""
            **Elvárt oszlopok a termékekhez:**
            - `SKU` (opcionális)
            - `Név` (kötelező)
            - `Egység` (pl. db)
            - `Min. készlet`
            - `Megjegyzés`
            
            Az import csak új termékeket ad hozzá (nem frissít meglévőket).
            """)
            
            uploaded = st.file_uploader("Excel fájl feltöltése", type=["xlsx", "xls"])
            if uploaded:
                try:
                    df_imp = pd.read_excel(uploaded)
                    st.write("Előnézet:")
                    st.dataframe(df_imp.head(10), use_container_width=True)
                    
                    if st.button("Importálás indítása"):
                        # Oszlopok normalizálása
                        col_map = {}
                        for col in df_imp.columns:
                            cl = str(col).lower().strip()
                            if "sku" in cl or "cikkszám" in cl or "cikkszam" in cl:
                                col_map["sku"] = col
                            elif "név" in cl or "nev" in cl or "name" in cl or "termék" in cl:
                                col_map["name"] = col
                            elif "egység" in cl or "egyseg" in cl or "unit" in cl:
                                col_map["unit"] = col
                            elif "min" in cl:
                                col_map["min_stock"] = col
                            elif "megj" in cl or "note" in cl:
                                col_map["note"] = col
                        
                        if "name" not in col_map:
                            st.error("Nem található 'Név' oszlop!")
                        else:
                            success = 0
                            errors = []
                            for _, row in df_imp.iterrows():
                                name = str(row[col_map["name"]]).strip()
                                if not name or name == "nan":
                                    continue
                                sku = str(row[col_map["sku"]]).strip() if "sku" in col_map else None
                                if sku == "nan":
                                    sku = None
                                unit = str(row[col_map["unit"]]).strip() if "unit" in col_map else "db"
                                if unit == "nan":
                                    unit = "db"
                                min_s = float(row[col_map["min_stock"]]) if "min_stock" in col_map else 0.0
                                note = str(row[col_map["note"]]).strip() if "note" in col_map else ""
                                if note == "nan":
                                    note = ""
                                
                                ok, msg = add_product(sku, name, unit, min_s, note)
                                if ok:
                                    success += 1
                                else:
                                    errors.append(f"{name}: {msg}")
                            
                            st.success(f"{success} termék sikeresen importálva.")
                            if errors:
                                with st.expander("Hibák"):
                                    for e in errors[:20]:
                                        st.write(e)
                            st.rerun()
                except Exception as e:
                    st.error(f"Hiba az Excel olvasásakor: {e}")
        
        st.markdown("---")
        st.subheader("🖨️ Nyomtatás")
        st.info("""
        **Hogyan nyomtass:**
        1. Menj a **Termékek** vagy **Mozgások napló** oldalra
        2. Nyomd meg a **Ctrl + P** (Windows) vagy **Cmd + P** (Mac) billentyűkombinációt
        3. Válaszd a „Mentés PDF-ként” opciót, ha fájlba szeretnéd
        """)

if __name__ == "__main__":
    main()
