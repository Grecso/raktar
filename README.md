# Raktárkezelő – Streamlit + Turso

Teljes funkciójú raktárkezelő webalkalmazás tartós Turso adatbázissal.

## Beállítás

### 1. Secrets fájl

Hozd létre a `.streamlit/secrets.toml` fájlt (a secrets.toml.example alapján):

```toml
[turso]
url = "libsql://raktar-grecso.aws-eu-west-1.turso.io"
token = "A_TE_TOKENED"
```

### 2. Telepítés és indítás

```bash
pip install -r requirements.txt
streamlit run app.py
```

### 3. Streamlit Cloud-ra feltöltés

1. Töltsd fel a mappát GitHub-ra (a secrets.toml **nélkül**!)
2. share.streamlit.io → Create app
3. A Streamlit Cloud **Secrets** menüjében add meg ugyanezt a [turso] blokkot

## Jelenlegi állapot (v1.0)

Kész:
- Turso kapcsolat + teljes séma
- Termékek (CRUD)
- Partnerek
- Bevételezés (batch + lokáció + mozgás)
- Készletnézet
- Mozgások napló
- Alap jelszó / zárolás keret
- Excel export (termékek + készlet)

Következő modulok (folytatásban):
- Megrendelések + allokáció (best-fit)
- Napi / havi zárás + díjszámítás
- Teljes riportok + grafikonok
- JSON biztonsági mentés
- Mintaadat generálás
- Fejlettebb jelszókezelés
