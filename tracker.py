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
    return spreadsheet.worksheet("Portfolio"), spreadsheet.worksheet("Storico"), spreadsheet.worksheet("Carte in osservazione")


def analyze_order_book(blueprint_id, language, target_conditions=None):
    endpoint = f"{BASE_URL}/marketplace/products"
    params = {"blueprint_id": blueprint_id, "language": language}
    
    try:
        response = requests.get(endpoint, headers=CT_HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            raw_products = response.json().get(str(blueprint_id), [])
            
            # 1. Filtriamo inserzioni attive
            valid_products = [
                p for p in raw_products 
                if not p.get("on_vacation", False) and p.get("quantity", 0) > 0
            ]
            
            # 2. SEZIONE OSSERVAZIONE: Filtro opzionale per condizione (NM o Mint)
            if target_conditions:
                valid_products = [
                    p for p in valid_products
                    if p.get("properties_hash", {}).get("condition") in target_conditions
                ]

            if not valid_products:
                return None
            
            # 3. Ordiniamo per prezzo
            valid_products.sort(key=lambda x: x.get("price", {}).get("cents", 0))
            
            # 4. Scartiamo outlier
            first_price = valid_products[0].get("price", {}).get("cents", 0) / 100.0
            filtered_products = [
                p for p in valid_products 
                if (p.get("price", {}).get("cents", 0) / 100.0) <= (first_price * 3.0)
            ]
            
            if not filtered_products:
                return None

            prices_eur = [p.get("price", {}).get("cents", 0) / 100.0 for p in filtered_products]
            quantities = [p.get("quantity", 1) for p in filtered_products]
            
            lowest_price = prices_eur[0]
            # Estrazione del nome venditore che detiene il prezzo più basso
            lowest_seller = filtered_products[0].get("user", {}).get("username", "N/A")
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
                "depth": market_depth,
                "lowest_seller": lowest_seller
            }
            
        elif response.status_code == 429:
            time.sleep(2)
            return analyze_order_book(blueprint_id, language, target_conditions)
            
        return None
        
    except Exception as e:
        print(f"Errore API: {e}")
        return None


def process_portfolio(ws_portfolio, ws_storico, date_only, timestamp_now):
    print("\n--- INIZIO ANALISI PORTFOLIO ---")
    rows = ws_portfolio.get_all_values()
    
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 12:
            row.extend([""] * (12 - len(row)))

        nome_prodotto = row[1]
        owned_language = row[2].strip().lower() or "it"
        blueprint_id = row[3].strip()

        if not blueprint_id:
            continue

        print(f"🔎 Portfolio: {nome_prodotto} (ID: {blueprint_id})...")
        # Per il portfolio non passiamo il target_conditions, calcola su tutto
        metrics_it = analyze_order_book(blueprint_id, "it")
        time.sleep(1.2)
        metrics_en = analyze_order_book(blueprint_id, "en")
        time.sleep(1.2)

        metrics_owned = metrics_it if owned_language == "it" else metrics_en

        if metrics_owned:
            ws_portfolio.update_cell(i, 8, metrics_owned["vwap"])
            ws_portfolio.update_cell(i, 12, timestamp_now)

        # Storicizzazione standard (Omissis stampe per brevità)
        val_it = metrics_it or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        val_en = metrics_en or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        
        spread = round(metrics_en["vwap"] - metrics_it["vwap"], 2) if metrics_en and metrics_it else "N/A"
        
        ws_storico.append_row([
            date_only, nome_prodotto, blueprint_id, 
            val_it["lowest_price"], val_it["vwap"], val_it["volatility"], val_it["depth"],
            val_en["lowest_price"], val_en["vwap"], val_en["volatility"], val_en["depth"], 
            spread, owned_language
        ])


def process_osservazione(ws_osservazione):
    print("\n--- INIZIO ANALISI CARTE IN OSSERVAZIONE (SOLO NM/MINT) ---")
    rows = ws_osservazione.get_all_values()
    
    # Condizioni target per l'osservazione speculativa
    condizioni_top = ["Near Mint", "Mint"]
    
    # Aggiornamenti batch per ridurre chiamate API a Google Sheets
    updates = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 14:
            row.extend([""] * (14 - len(row)))

        nome_prodotto = row[0]
        blueprint_id = row[2].strip()

        if not blueprint_id:
            continue

        print(f"🎯 Osservazione: {nome_prodotto} (ID: {blueprint_id})...")
        
        # Passiamo le condizioni target
        metrics_it = analyze_order_book(blueprint_id, "it", target_conditions=condizioni_top)
        time.sleep(1.2)
        metrics_en = analyze_order_book(blueprint_id, "en", target_conditions=condizioni_top)
        time.sleep(1.2)

        # Preparazione dati IT
        if metrics_it:
            updates.append({"range": f"D{i}:H{i}", "values": [[
                metrics_it["lowest_price"], metrics_it["vwap"], metrics_it["volatility"], 
                metrics_it["depth"], metrics_it["lowest_seller"]
            ]]})
        
        # Preparazione dati EN
        if metrics_en:
            updates.append({"range": f"I{i}:M{i}", "values": [[
                metrics_en["lowest_price"], metrics_en["vwap"], metrics_en["volatility"], 
                metrics_en["depth"], metrics_en["lowest_seller"]
            ]]})
            
        # Calcolo Spread
        if metrics_it and metrics_en:
            spread = round(metrics_en["vwap"] - metrics_it["vwap"], 2)
            updates.append({"range": f"N{i}", "values": [[spread]]})

    # Scrittura in batch su Google Sheets per massima efficienza
    if updates:
        ws_osservazione.batch_update(updates)
        print(f"✅ Foglio Osservazione aggiornato con {len(updates)} range di celle.")


def update_system():
    ws_portfolio, ws_storico, ws_osservazione = setup_google_sheets()
    
    timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_only = datetime.now().strftime("%Y-%m-%d")

    process_portfolio(ws_portfolio, ws_storico, date_only, timestamp_now)
    process_osservazione(ws_osservazione)

    print("\n✅ Elaborazione globale completata!")

if __name__ == "__main__":
    print("🚀 Avvio Pokemon Market Intelligence...")
    update_system()
