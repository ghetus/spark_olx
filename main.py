import os
import json
import time
from datetime import datetime
import requests
import gspread
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# CONFIGURARE
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Proiectoare OLX")

# Endpoint-ul API intern OLX (mult mai stabil și greu de blocat decât parsarea HTML)
OLX_API_URL = "https://www.olx.ro/api/v1/offers/?category_id=745&sort_by=created_at:desc&limit=40"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.olx.ro/d/electronice-electrocasnice/tv-audio-video/videoproiectoare/",
    "Origin": "https://www.olx.ro"
}

# ---------------------------------------------------------------------------
# PROMPT GEMINI (Pentru filtrare & calculat scor)
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """
Ești un expert tehnic în echipamente video și videoproiectoare. 
Analizează titlul, parametrii și descrierea furnizate din anunțul OLX pentru a determina dacă videoproiectorul respectă STRICT criteriile următoare:

CRITERII TEHNICE OBLIGATORII:
1. Luminozitate: minim 3000 ANSI Lumens (sau echivalent de producție verificat pentru modelul respectiv).
2. Rezoluție nativă: minim 1280x800 (WXGA, HD+, Full HD 1080p, 2K, 4K).
3. Raport aspect nativ: 16:9 sau 16:10 (nu 4:3).
4. An fabricație / Lansare model: aproximativ 2020 sau mai nou.
5. Conectivitate: Port HDMI prezent.
6. Stare: În stare perfectă/bună de funcționare (fără lămpi arse, pete pe imagine sau defecte majore).

Dacă specificațiile tehnice complete nu sunt scrise explicit în descriere, folosește-ți cunoștințele tehnice despre modelul identificat pentru a verifica fișa tehnică a producătorului.

Trebuie să returnezi UNICEMENTE un obiect JSON cu această structură:
{
  "eligible": true/false,
  "reject_reason": "Motivul respingerii (dacă eligible este false)",
  "brand_model": "Nume Brand și Model identificat",
  "specs": "Rezoluție nativă | Lumeni ANSI | Aspect | An lansare",
  "quality_score": "X/10 - O scurtă argumentare privind raportul calitate/preț în raport cu piața second-hand",
  "summary": "Scurtă descriere a stării și dotărilor din anunț"
}
"""

def init_gemini():
    """Inițializează clientul Gemini."""
    if not GEMINI_API_KEY:
        raise ValueError("Lipsește variabila de mediu GEMINI_API_KEY!")
    return genai.Client(api_key=GEMINI_API_KEY)

def init_google_sheets():
    """Autentificare în Google Sheets folosind contul de serviciu."""
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("Lipsește variabila de mediu GOOGLE_CREDENTIALS_JSON!")
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open(SPREADSHEET_NAME)
    worksheet = sh.sheet1
    
    existing_headers = worksheet.row_values(1)
    expected_headers = [
        "Data Inserării", 
        "Titlu Anunț", 
        "Brand & Model", 
        "Detalii Tehnice", 
        "Preț", 
        "Nota Calitate/Preț", 
        "Sumar / Stare", 
        "Link Anunț"
    ]
    if not existing_headers:
        worksheet.append_row(expected_headers)
        
    return worksheet

def get_already_inserted_links(worksheet):
    """Obține lista de link-uri deja procesate pentru a evita duplicatele."""
    try:
        col_links = worksheet.col_values(8)
        return set(col_links[1:])
    except Exception as e:
        print(f"Avertisment la citirea linkurilor existente: {e}")
        return set()

def fetch_olx_ads():
    """Preluare anunțuri prin API-ul intern JSON OLX."""
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Efectuăm o cerere către homepage pentru a prelua cookie-urile de sesiune
    try:
        session.get("https://www.olx.ro", timeout=10)
    except Exception:
        pass
    
    response = session.get(OLX_API_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    
    offers = data.get("data", [])
    ads = []
    
    for offer in offers:
        title = offer.get("title", "")
        url = offer.get("url", "")
        
        # Formatare preț
        params = offer.get("params", [])
        price_str = "Nespecificat"
        for p in params:
            if p.get("key") == "price":
                val = p.get("value", {})
                price_str = f"{val.get('value', '')} {val.get('currency', 'RON')}"
                break
                
        desc = offer.get("description", "")
        # Eliminare tag-uri html simple din descriere
        desc_clean = desc.replace("<br />", "\n").replace("<br/>", "\n").replace("<p>", "").replace("</p>", "\n")
        
        ads.append({
            "title": title,
            "link": url,
            "price": price_str,
            "description": desc_clean
        })
        
    return ads

def analyze_ad_with_gemini(client, title, price, description):
    """Analizează anunțul cu Gemini API folosind JSON structurat."""
    prompt = f"""
    Titlu Anunț: {title}
    Preț solicitat: {price}
    Descriere din anunț:
    {description}
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Eroare la analiza Gemini: {e}")
        return None

def main():
    print("Pornește automatizarea verificării anunțurilor OLX...")
    
    gemini_client = init_gemini()
    worksheet = init_google_sheets()
    inserted_links = get_already_inserted_links(worksheet)
    
    ads = fetch_olx_ads()
    print(f"S-au recepționat {len(ads)} anunțuri de pe OLX.")
    
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    inserted_count = 0
    
    for ad in ads:
        if ad["link"] in inserted_links:
            continue
            
        print(f"\nAnalizare: {ad['title']} ({ad['price']})")
        analysis = analyze_ad_with_gemini(
            client=gemini_client,
            title=ad["title"],
            price=ad["price"],
            description=ad["description"]
        )
        
        if not analysis:
            continue
            
        if analysis.get("eligible") is True:
            print(" -> ACCEPTAT! Se inserează în Google Sheets...")
            row = [
                today_str,
                ad["title"],
                analysis.get("brand_model", "N/A"),
                analysis.get("specs", "N/A"),
                ad["price"],
                analysis.get("quality_score", "N/A"),
                analysis.get("summary", "N/A"),
                ad["link"]
            ]
            worksheet.append_row(row)
            inserted_links.add(ad["link"])
            inserted_count += 1
        else:
            reason = analysis.get("reject_reason", "Neeligibil")
            print(f" -> Respins: {reason}")
            
        time.sleep(1)  # Pauză scurtă între interogări

    print(f"\nFinalizat cu succes! S-au adăugat {inserted_count} anunțuri noi eligibile.")

if __name__ == "__main__":
    main()
