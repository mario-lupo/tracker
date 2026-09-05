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
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open(SPREADSHEET_NAME)
    
    return (
        spreadsheet.worksheet("Portfolio"), 
        spreadsheet.worksheet("Storico"), 
        spreadsheet.worksheet("Target ITA/ENG"),
        spreadsheet.worksheet("Carte in osservazione (ITA/ENG)"),
        spreadsheet.worksheet("Target JAP"),
        spreadsheet.worksheet("Carte in osservazione JAP"),
        spreadsheet.worksheet("Target CHI"),
        spreadsheet.worksheet("Carte in osservazione CHI")
    )


def analyze_order_book(blueprint_id, language, target_conditions=None):
    endpoint = f"{BASE_URL}/marketplace/products"
    params = {"blueprint_id": blueprint_id, "language": language}
    
    try:
        response = requests.get(endpoint, headers=CT_HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            raw_products = response.json().get(str(blueprint_id), [])
            
            # Filtro base: non in vacanza e con quantità > 0
            valid_products = [
                p for p in raw_products 
                if not p.get("on_vacation", False) and p.get("quantity", 0) > 0
            ]
            
            # Filtro condizioni (Near Mint / Mint) per i target di osservazione
            if target_conditions:
                valid_products = [
                    p for p in valid_products
                    if p.get("properties_hash", {}).get("condition") in target_conditions
                ]

            if not valid_products:
                return None
            
            valid_products.sort(key=lambda x: x.get("price", {}).get("cents", 0))
            
            # Filtro outlier (prendiamo solo carte che costano fino al triplo della più economica)
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
            lowest_seller = filtered_products[0].get("user", {}).get("username", "N/A")
            market_depth = sum(quantities)
            
            # Calcolo VWAP sui primi 5 venditori
            top_5_prices = prices_eur[:5]
            top_5_qtys = quantities[:5]
            total_top_qty = sum(top_5_qtys)
            vwap = sum(p * q for p, q in zip(top_5_prices, top_5_qtys)) / float(total_top_qty) if total_top_qty > 0 else lowest_price
            
            # Calcolo Volatilità sui primi 10 venditori
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
    
    rows_to_append = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 12:
            row.extend([""] * (12 - len(row)))

        nome_prodotto = row[1]
        owned_language = row[2].strip().lower() or "it"
        blueprint_id = row[3].strip()

        if not blueprint_id:
            continue

        print(f"🔎 Portfolio: {nome_prodotto} (ID: {blueprint_id})...")
        metrics_it = analyze_order_book(blueprint_id, "it")
        time.sleep(1.2)
        metrics_en = analyze_order_book(blueprint_id, "en")
        time.sleep(1.2)

        metrics_owned = metrics_it if owned_language == "it" else metrics_en

        if metrics_owned:
            # Aggiorna Prezzo Spot e Ultimo Aggiornamento nel foglio Portfolio
            ws_portfolio.update_cell(i, 8, metrics_owned["vwap"])
            ws_portfolio.update_cell(i, 12, timestamp_now)

        val_it = metrics_it or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        val_en = metrics_en or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        
        spread = round(metrics_en["vwap"] - metrics_it["vwap"], 2) if metrics_en and metrics_it else "N/A"
        
        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id, 
            val_it["lowest_price"], val_it["vwap"], val_it["volatility"], val_it["depth"],
            val_en["lowest_price"], val_en["vwap"], val_en["volatility"], val_en["depth"], 
            spread, owned_language
        ])

    if rows_to_append:
        rows_to_append.append([]) # Inserisce la riga vuota di separazione giornaliera
        ws_storico.append_rows(rows_to_append)
        print("✅ Storico Portfolio aggiornato con successo.")


def process_osservazione_it_en(ws_target, ws_log, date_only):
    print("\n--- OSSERVAZIONE (ITA/ENG - NM/MINT) ---")
    rows = ws_target.get_all_values()
    condizioni_top = ["Near Mint", "Mint"]
    rows_to_append = []

    for row in rows[1:]:
        if len(row) < 2:
            continue

        nome_prodotto = row[0]
        blueprint_id = row[1].strip()

        if not blueprint_id:
            continue

        print(f"🎯 Target ITA/ENG: {nome_prodotto} (ID: {blueprint_id})...")
        
        metrics_it = analyze_order_book(blueprint_id, "it", target_conditions=condizioni_top)
        time.sleep(1.2)
        metrics_en = analyze_order_book(blueprint_id, "en", target_conditions=condizioni_top)
        time.sleep(1.2)

        val_it = metrics_it or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A", "lowest_seller": "N/A"}
        val_en = metrics_en or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A", "lowest_seller": "N/A"}

        spread = round(metrics_en["vwap"] - metrics_it["vwap"], 2) if metrics_it and metrics_en else "N/A"

        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id,
            val_it["lowest_price"], val_it["vwap"], val_it["volatility"], val_it["depth"], val_it["lowest_seller"],
            val_en["lowest_price"], val_en["vwap"], val_en["volatility"], val_en["depth"], val_en["lowest_seller"],
            spread
        ])

    if rows_to_append:
        rows_to_append.append([]) # Inserisce la riga vuota di separazione giornaliera
        ws_log.append_rows(rows_to_append)
        print("✅ Storico Osservazione ITA/ENG aggiornato.")


def process_osservazione_asiatica(ws_target, ws_log, date_only, language_code, section_name):
    print(f"\n--- OSSERVAZIONE ({section_name} - NM/MINT) ---")
    rows = ws_target.get_all_values()
    condizioni_top = ["Near Mint", "Mint"]
    rows_to_append = []

    for row in rows[1:]:
        if len(row) < 2:
            continue

        nome_prodotto = row[0]
        blueprint_id = row[1].strip()

        if not blueprint_id:
            continue

        print(f"🎯 Target {section_name}: {nome_prodotto} (ID: {blueprint_id})...")
        
        metrics = analyze_order_book(blueprint_id, language_code, target_conditions=condizioni_top)
        time.sleep(1.2)

        val = metrics or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A", "lowest_seller": "N/A"}

        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id,
            val["lowest_price"], val["vwap"], val["volatility"], val["depth"], val["lowest_seller"]
        ])

    if rows_to_append:
        rows_to_append.append([]) # Inserisce la riga vuota di separazione giornaliera
        ws_log.append_rows(rows_to_append)
        print(f"✅ Storico Osservazione {section_name} aggiornato.")


def update_system():
    # Setup connessione a Google Sheets
    sheets = setup_google_sheets()
    
    timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_only = datetime.now().strftime("%Y-%m-%d")

    # 1. Processo Portfolio -> Aggiorna Portfolio e salva in Storico
    process_portfolio(sheets[0], sheets[1], date_only, timestamp_now)
    
    # 2. Processo Osservazione ITA/ENG
    process_osservazione_it_en(sheets[2], sheets[3], date_only)
    
    # 3. Processo Osservazione Giapponese (jp)
    process_osservazione_asiatica(sheets[4], sheets[5], date_only, "jp", "JAP")
    
    # 4. Processo Osservazione Cinese Semplificato (cn)
    process_osservazione_asiatica(sheets[6], sheets[7], date_only, "cn", "CHI")

    print("\n🚀 Elaborazione e storicizzazione globale completate con successo!")


if __name__ == "__main__":
    print("Avvio Pokemon Market Intelligence...")
    update_system()
