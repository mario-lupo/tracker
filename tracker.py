import math
import time
from datetime import datetime
import os
import json
import gspread
import requests
from google.oauth2.service_account import Credentials
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# --- CONFIGURAZIONE GITHUB SECRETS ---
CARDTRADER_TOKEN = os.environ.get("CARDTRADER_TOKEN") 
creds_dict = json.loads(os.environ.get("GCP_CREDENTIALS"))
SPREADSHEET_NAME = "Pokemon_Tracker"
BASE_URL = "https://api.cardtrader.com/api/v2"

# --- COSTANTI ALGORITMO ---
CONDITIONS_HIERARCHY = ["Mint", "Near Mint", "Slightly Played", "Moderately Played", "Played", "Poor"]
MAX_PRICE_CAP = 200.00


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


def parse_bool(value):
    """Converte 'SI', 'TRUE', '1' in booleano True, altrimenti False."""
    if isinstance(value, bool): return value
    if not value: return False
    return str(value).strip().upper() in ("SI", "SÌ", "TRUE", "1", "YES", "Y")


@retry(
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5)
)
def fetch_api(endpoint, params, session):
    """Chiamata API con Exponential Backoff per evitare ban 429."""
    response = session.get(endpoint, params=params, timeout=10)
    if response.status_code == 429:
        raise requests.exceptions.RequestException("Rate Limit Hit (429)")
    if response.status_code == 200:
        return response.json()
    return {}


def analyze_order_book_portfolio(blueprint_id, language, session):
    """Analisi standard per il portfolio (gestisce sia carte che sigillati senza filtri di condizione)."""
    endpoint = f"{BASE_URL}/marketplace/products"
    params = {"blueprint_id": blueprint_id, "language": language}
    
    try:
        response_data = fetch_api(endpoint, params, session)
        raw_products = response_data.get(str(blueprint_id), [])
        
        valid_products = [
            p for p in raw_products 
            if not p.get("on_vacation", False) and p.get("quantity", 0) > 0
            and not p.get("user", {}).get("too_many_request_for_cancel_as_seller", False)
        ]
        
        if not valid_products:
            return None
            
        valid_products.sort(key=lambda x: x.get("price", {}).get("cents", 0))
        
        # Filtro outlier (fino al triplo del più economico)
        first_price = valid_products[0].get("price", {}).get("cents", 0) / 100.0
        filtered_products = [
            p for p in valid_products 
            if (p.get("price", {}).get("cents", 0) / 100.0) <= (first_price * 3.0)
        ]
        
        if not filtered_products:
            filtered_products = [valid_products[0]]

        prices_eur = [p.get("price", {}).get("cents", 0) / 100.0 for p in filtered_products]
        quantities = [p.get("quantity", 1) for p in filtered_products]
        
        lowest_price = prices_eur[0]
        market_depth = sum(quantities)
        
        # VWAP sui primi 5 venditori filtrati
        top_5_prices = prices_eur[:5]
        top_5_qtys = quantities[:5]
        total_top_qty = sum(top_5_qtys)
        vwap = sum(p * q for p, q in zip(top_5_prices, top_5_qtys)) / float(total_top_qty) if total_top_qty > 0 else lowest_price
        
        # Volatilità sui primi 10
        top_10_prices = prices_eur[:10]
        mean_top_10 = sum(top_10_prices) / len(top_10_prices)
        variance = sum((x - mean_top_10) ** 2 for x in top_10_prices) / len(top_10_prices)
        volatility = math.sqrt(variance)
        
        return {
            "lowest_price": round(lowest_price, 2),
            "vwap": round(vwap, 2),
            "volatility": round(volatility, 2),
            "depth": market_depth
        }
    except Exception as e:
        print(f"Errore Portfolio API su {blueprint_id}: {e}")
        return None


def analyze_order_book_advanced(blueprint_id, language, is_reverse, is_first_ed, session, max_cap=MAX_PRICE_CAP):
    """Algoritmo avanzato per l'Osservazione: Floor, Total Depth, Target Condizione, Inferiore e Intorno."""
    endpoint = f"{BASE_URL}/marketplace/products"
    params = {"blueprint_id": blueprint_id, "language": language}
    
    try:
        response_data = fetch_api(endpoint, params, session)
        raw_products = response_data.get(str(blueprint_id), [])
        
        if not raw_products:
            return None
            
        # 1. HARD FILTERS
        valid_products = []
        for p in raw_products:
            user = p.get("user", {})
            props = p.get("properties_hash", {})
            
            if p.get("on_vacation", False) or p.get("quantity", 0) <= 0:
                continue
            if user.get("too_many_request_for_cancel_as_seller", False):
                continue
                
            item_reverse = bool(props.get("reverse_holo", False))
            item_first = bool(props.get("first_edition", False))
            
            if item_reverse != is_reverse or item_first != is_first_ed:
                continue
                
            valid_products.append(p)
            
        if not valid_products:
            return None

        # Metriche Macro Globali (su tutto l'ordinato valido)
        valid_products.sort(key=lambda x: x.get("price", {}).get("cents", 0))
        floor_price = valid_products[0].get("price", {}).get("cents", 0) / 100.0
        total_depth = sum([p.get("quantity", 1) for p in valid_products])

        # 2. DEGRADAZIONE CONDIZIONE & BUDGET CAP per il Target
        selected_condition = "N/A"
        target_cond_listings = []
        
        for cond in CONDITIONS_HIERARCHY:
            cond_listings = [
                p for p in valid_products 
                if p.get("properties_hash", {}).get("condition", "") == cond
            ]
            if not cond_listings:
                continue
            min_price_in_cond = min([p.get("price", {}).get("cents", 0) / 100.0 for p in cond_listings])
            if min_price_in_cond <= max_cap:
                selected_condition = cond
                target_cond_listings = cond_listings
                break
                
        if not target_cond_listings:
            # Fallback se nessuna condizione rispetta il cap sotto i 200€
            selected_condition = valid_products[0].get("properties_hash", {}).get("condition", "N/A")
            target_cond_listings = [p for p in valid_products if p.get("properties_hash", {}).get("condition", "") == selected_condition]

        # 3. VENDOR TRADE-OFF (Gruppo A: Italia o CT Zero vs Gruppo B: Estero)
        group_a = []
        group_b = []
        for p in target_cond_listings:
            user = p.get("user", {})
            if user.get("country_code", "").upper() == "IT" or user.get("can_sell_via_hub", False):
                group_a.append(p)
            else:
                group_b.append(p)
                
        winner = min(group_a, key=lambda x: x.get("price", {}).get("cents", 0)) if group_a else min(group_b, key=lambda x: x.get("price", {}).get("cents", 0))
        
        winner_price = winner.get("price", {}).get("cents", 0) / 100.0
        winner_user = winner.get("user", {})
        
        # 4. VWAP e Volatilità sul Target Condizione
        target_prices = [p.get("price", {}).get("cents", 0) / 100.0 for p in target_cond_listings]
        target_qtys = [p.get("quantity", 1) for p in target_cond_listings]
        target_depth = sum(target_qtys)
        
        target_vwap = sum(p * q for p, q in zip(target_prices, target_qtys)) / float(target_depth) if target_depth > 0 else winner_price
        
        if len(target_prices) > 1:
            mean_tp = sum(target_prices) / len(target_prices)
            variance = sum((x - mean_tp) ** 2 for x in target_prices) / len(target_prices)
            target_volatility = math.sqrt(variance)
        else:
            target_volatility = 0.0

        # 5. CONDIZIONE INFERIORE & SPREAD
        lower_condition_min = "N/A"
        spread_percentage = "N/A"
        
        try:
            current_cond_index = CONDITIONS_HIERARCHY.index(selected_condition)
            for lower_cond in CONDITIONS_HIERARCHY[current_cond_index + 1:]:
                lower_listings = [p for p in valid_products if p.get("properties_hash", {}).get("condition", "") == lower_cond]
                if lower_listings:
                    lower_condition_min = round(min([p.get("price", {}).get("cents", 0) / 100.0 for p in lower_listings]), 2)
                    break
            if isinstance(lower_condition_min, (int, float)) and lower_condition_min > 0:
                spread_percentage = round(((winner_price - lower_condition_min) / lower_condition_min) * 100.0, 2)
        except ValueError:
            pass

        # 6. MEDIA PROSSIMI 5 ANNUNCI (Escludendo il vincitore attuale)
        other_listings = [p for p in valid_products if p != winner]
        other_prices = [p.get("price", {}).get("cents", 0) / 100.0 for p in other_listings]
        next_5_prices = other_prices[:5]
        media_next_5 = round(sum(next_5_prices) / len(next_5_prices), 2) if next_5_prices else "N/A"

        return {
            "Floor": round(floor_price, 2),
            "TotalDepth": total_depth,
            "Condizione": selected_condition,
            "Minimo": round(winner_price, 2),
            "VWAP": round(target_vwap, 2),
            "Volatilità": round(target_volatility, 2),
            "Depth": target_depth,
            "MinimoInferiore": lower_condition_min,
            "SpreadInferiore": spread_percentage,
            "MediaNext5": media_next_5,
            "Paese": winner_user.get("country_code", "N/A").upper(),
            "Tipo": "PRO" if winner_user.get("user_type") == "pro" else "Normal",
            "Venditore": winner_user.get("username", "N/A"),
            "CT Zero": "SI" if winner_user.get("can_sell_via_hub", False) else "NO"
        }
    except Exception as e:
        print(f"Errore Advanced API su {blueprint_id}: {e}")
        return None


def process_portfolio(ws_portfolio, ws_storico, date_only, timestamp_now, session):
    print("\n--- INIZIO ANALISI PORTFOLIO ---")
    rows = ws_portfolio.get_all_values()
    rows_to_append = []

    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 12:
            row.extend([""] * (12 - len(row)))

        nome_prodotto = row[1]
        owned_language = row[2].strip().lower() or "it"
        blueprint_id = row[3].strip()

        if not blueprint_id: continue

        print(f"🔎 Portfolio: {nome_prodotto} (ID: {blueprint_id})...")
        metrics_it = analyze_order_book_portfolio(blueprint_id, "it", session)
        time.sleep(1.0)
        metrics_en = analyze_order_book_portfolio(blueprint_id, "en", session)
        time.sleep(1.0)

        metrics_owned = metrics_it if owned_language == "it" else metrics_en
        if metrics_owned:
            ws_portfolio.update_cell(i, 8, metrics_owned["vwap"])
            ws_portfolio.update_cell(i, 12, timestamp_now)

        val_it = metrics_it or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        val_en = metrics_en or {"lowest_price": "N/A", "vwap": "N/A", "volatility": "N/A", "depth": "N/A"}
        
        spread = round(val_en["vwap"] - val_it["vwap"], 2) if metrics_en and metrics_it and val_en["vwap"] != "N/A" and val_it["vwap"] != "N/A" else "N/A"
        
        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id, 
            val_it["lowest_price"], val_it["vwap"], val_it["volatility"], val_it["depth"],
            val_en["lowest_price"], val_en["vwap"], val_en["volatility"], val_en["depth"], 
            spread, owned_language
        ])

    if rows_to_append:
        rows_to_append.append([]) 
        ws_storico.append_rows(rows_to_append, value_input_option='USER_ENTERED')
        print("✅ Storico Portfolio aggiornato con successo.")


def process_osservazione_it_en(ws_target, ws_log, date_only, session):
    print("\n--- OSSERVAZIONE (ITA/ENG) ---")
    rows = ws_target.get_all_values()
    rows_to_append = []

    for row in rows[1:]:
        if len(row) < 4:
            row.extend([""] * (4 - len(row)))
            
        nome_prodotto, blueprint_id = row[0], row[1].strip()
        is_reverse = parse_bool(row[2])
        is_first_ed = parse_bool(row[3])

        if not blueprint_id: continue

        print(f"🎯 Target ITA/ENG: {nome_prodotto} (ID: {blueprint_id})...")
        metrics_it = analyze_order_book_advanced(blueprint_id, "it", is_reverse, is_first_ed, session)
        time.sleep(1.0)
        metrics_en = analyze_order_book_advanced(blueprint_id, "en", is_reverse, is_first_ed, session)
        time.sleep(1.0)

        null_res = {k: "N/A" for k in ["Floor", "TotalDepth", "Condizione", "Minimo", "VWAP", "Volatilità", "Depth", "MinimoInferiore", "SpreadInferiore", "MediaNext5", "Paese", "Tipo", "Venditore", "CT Zero"]}
        v_it = metrics_it or null_res
        v_en = metrics_en or null_res

        spread = round(v_en["Minimo"] - v_it["Minimo"], 2) if metrics_it and metrics_en and v_en["Minimo"] != "N/A" and v_it["Minimo"] != "N/A" else "N/A"

        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id,
            v_it["Floor"], v_it["TotalDepth"], v_it["Condizione"], v_it["Minimo"], v_it["VWAP"], v_it["Volatilità"], v_it["Depth"], v_it["MinimoInferiore"], v_it["SpreadInferiore"], v_it["MediaNext5"], v_it["Paese"], v_it["Tipo"], v_it["Venditore"], v_it["CT Zero"],
            v_en["Floor"], v_en["TotalDepth"], v_en["Condizione"], v_en["Minimo"], v_en["VWAP"], v_en["Volatilità"], v_en["Depth"], v_en["MinimoInferiore"], v_en["SpreadInferiore"], v_en["MediaNext5"], v_en["Paese"], v_en["Tipo"], v_en["Venditore"], v_en["CT Zero"],
            spread
        ])

    if rows_to_append:
        rows_to_append.append([]) 
        ws_log.append_rows(rows_to_append, value_input_option='USER_ENTERED')
        print("✅ Storico Osservazione ITA/ENG aggiornato.")


def process_osservazione_asiatica(ws_target, ws_log, date_only, language_code, section_name, session):
    print(f"\n--- OSSERVAZIONE ({section_name}) ---")
    rows = ws_target.get_all_values()
    rows_to_append = []

    for row in rows[1:]:
        if len(row) < 4:
            row.extend([""] * (4 - len(row)))

        nome_prodotto, blueprint_id = row[0], row[1].strip()
        is_reverse = parse_bool(row[2])
        is_first_ed = parse_bool(row[3])

        if not blueprint_id: continue

        print(f"🎯 Target {section_name}: {nome_prodotto} (ID: {blueprint_id})...")
        metrics = analyze_order_book_advanced(blueprint_id, language_code, is_reverse, is_first_ed, session)
        time.sleep(1.0)

        null_res = {k: "N/A" for k in ["Floor", "TotalDepth", "Condizione", "Minimo", "VWAP", "Volatilità", "Depth", "MinimoInferiore", "SpreadInferiore", "MediaNext5", "Paese", "Tipo", "Venditore", "CT Zero"]}
        v = metrics or null_res

        rows_to_append.append([
            date_only, nome_prodotto, blueprint_id,
            v["Floor"], v["TotalDepth"], v["Condizione"], v["Minimo"], v["VWAP"], v["Volatilità"], v["Depth"], v["MinimoInferiore"], v["SpreadInferiore"], v["MediaNext5"], v["Paese"], v["Tipo"], v["Venditore"], v["CT Zero"]
        ])

    if rows_to_append:
        rows_to_append.append([]) 
        ws_log.append_rows(rows_to_append, value_input_option='USER_ENTERED')
        print(f"✅ Storico Osservazione {section_name} aggiornato.")


def update_system():
    sheets = setup_google_sheets()
    
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {CARDTRADER_TOKEN}"})
    
    timestamp_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_only = datetime.now().strftime("%Y-%m-%d")

    # 1. Processo Portfolio (Funzione pulita e originale)
    process_portfolio(sheets[0], sheets[1], date_only, timestamp_now, session)
    
    # 2. Processo Osservazione ITA/ENG (Framework Avanzato)
    process_osservazione_it_en(sheets[2], sheets[3], date_only, session)
    
    # 3. Processo Osservazione Giapponese (jp)
    process_osservazione_asiatica(sheets[4], sheets[5], date_only, "jp", "JAP", session)
    
    # 4. Processo Osservazione Cinese (cn)
    process_osservazione_asiatica(sheets[6], sheets[7], date_only, "cn", "CHI", session)

    print("\n🚀 Elaborazione e storicizzazione globale completate con successo!")


if __name__ == "__main__":
    print("Avvio Pokemon Market Intelligence...")
    update_system()
