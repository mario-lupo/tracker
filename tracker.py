import math
import time
from datetime import datetime
import os
import json
import gspread
import requests
from google.oauth2.service_account import Credentials

# --- CONFIGURAZIONE ---
CARDTRADER_TOKEN = os.environ.get("CARDTRADER_TOKEN")
creds_dict = json.loads(os.environ.get("GCP_CREDENTIALS"))
SPREADSHEET_NAME = "Pokemon_Tracker"
BASE_URL = "https://api.cardtrader.com/api/v2"
CT_HEADERS = {"Authorization": f"Bearer {CARDTRADER_TOKEN}"}


def setup_google_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        creds_dict, scopes=scopes
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open(SPREADSHEET_NAME)
    return spreadsheet.worksheet("Portfolio"), spreadsheet.worksheet("Storico")


def analyze_order_book(blueprint_id, language):
    endpoint = f"{BASE_URL}/marketplace/products"
    params = {"blueprint_id": blueprint_id, "language": language}
    
    try:
        response = requests.get(endpoint, headers=CT_HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            raw_products = response.json().get(str(blueprint_id), [])
            
            # 1. Filtriamo solo inserzioni attive
            valid_products = [
                p for p in raw_products 
                if not p.get("on_vacation", False) and p.get("quantity", 0) > 0
            ]
            
            if not valid_products:
                return None
            
            # 2. FIX FONDAMENTALE: Ordiniamo esplicitamente le inserzioni dal prezzo più basso al più alto
            valid_products.sort(key=lambda x: x.get("price", {}).get("cents", 0))
            
            # 3. FIX OUTLIER: Scartiamo lotti/case con prezzo > 3 volte il prezzo minimo
            first_price = valid_products[0].get("price", {}).get("cents", 0) / 100.0
            filtered_products = [
                p for p in valid_products 
                if (p.get("price", {}).get("cents", 0) / 100.0) <= (first_price * 3.0)
            ]
            
            prices_eur = [p.get("price", {}).get("cents", 0) / 100.0 for p in filtered_products]
            quantities = [p.get("quantity", 1) for p in filtered_products]
            
            lowest_price = prices_eur[0]
            market_depth = sum(quantities)
            
            top_5_prices = prices_eur[:5]
            top_5_qtys = quantities[:5]
            total_top_qty = sum(top_5_qtys)
            
            vwap = sum(p * q for p, q in zip(top_5_prices, top_5_qtys)) / float(total_top_qty) if total_top_qty > 0 else lowest_price
            
            top_10_prices = prices_eur[:10]
            mean_top_10 = sum(top_10_prices) / len(top_10_prices)
            variance = sum((x - mean_top_10) ** 2 for x in top_10_prices) / len(top_10_prices)
            floor_volatility = math.sqrt(variance)
            
            return {
                "lowest_price": round(lowest_price, 2),
                "vwap": round(vwap, 2),
                "volatility": round(floor_volatility, 2),
                "depth": market_depth
            }
            
        elif response.status_code == 429:
            time.sleep(2)
            return analyze_order_book(blueprint_id, language)
            
        return None
        
    except Exception as e:
        print(f"Errore API: {e}")
        return None


def update_system():
    ws_portfolio, ws_storico = setup_google_sheets()
    rows = ws_portfolio.get_all_values()

    timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_only = datetime.now().strftime("%Y-%m-%d")

    # Scorre le righe del foglio Portfolio (escludendo l'intestazione)
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 12:
            row.extend([""] * (12 - len(row)))

        nome_prodotto = row[1]
        owned_language = row[2].strip().lower() or "it"
        blueprint_id = row[3].strip()

        if not blueprint_id:
            continue

        print(f"\n🔎 Analisi: {nome_prodotto} (Blueprint: {blueprint_id})...")

        # Analisi mercato Italiano e Inglese
        metrics_it = analyze_order_book(blueprint_id, "it")
        time.sleep(1.2)

        metrics_en = analyze_order_book(blueprint_id, "en")
        time.sleep(1.2)

        # Seleziona la metrica della lingua effettivamente posseduta
        metrics_owned = metrics_it if owned_language == "it" else metrics_en

        # 1. AGGIORNAMENTO PORTFOLIO (Snapshot)
        if metrics_owned:
            ws_portfolio.update_cell(i, 8, metrics_owned["vwap"])
            ws_portfolio.update_cell(i, 12, timestamp_now)
            print(
                f"  └─ Portfolio aggiornato: VWAP Spot = €{metrics_owned['vwap']}"
            )
        else:
            print("  └─ ⚠️ Nessun dato disponibile per la lingua posseduta.")

        # 2. REGISTRAZIONE STORICO (Time-Series)
        val_it = metrics_it or {
            "lowest_price": "N/A",
            "vwap": "N/A",
            "volatility": "N/A",
            "depth": "N/A",
        }
        val_en = metrics_en or {
            "lowest_price": "N/A",
            "vwap": "N/A",
            "volatility": "N/A",
            "depth": "N/A",
        }

        # Calcolo Spread tra VWAP EN e VWAP IT
        if (
            metrics_en
            and metrics_it
            and isinstance(metrics_en["vwap"], (int, float))
            and isinstance(metrics_it["vwap"], (int, float))
        ):
            spread = round(metrics_en["vwap"] - metrics_it["vwap"], 2)
        else:
            spread = "N/A"

        historical_row = [
            date_only,
            nome_prodotto,
            blueprint_id,
            val_it["lowest_price"],
            val_it["vwap"],
            val_it["volatility"],
            val_it["depth"],
            val_en["lowest_price"],
            val_en["vwap"],
            val_en["volatility"],
            val_en["depth"],
            spread,
            owned_language,
        ]

        ws_storico.append_row(historical_row)
        print(f"  └─ Registro aggiunto nello Storico (Spread EN/IT: {spread}€)")

    print("\n✅ Elaborazione e storicizzazione completate con successo!")


if __name__ == "__main__":
    print("🚀 Avvio Pokemon Market Intelligence...")
    update_system()
