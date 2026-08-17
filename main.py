import os
import json
import time
from datetime import datetime
from curl_cffi import requests
import gspread
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# CONFIGURARE
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Proiectoare OLX")

OLX_API_URL = "https://www.olx.ro/api/v1/offers/?query=videoproiector&sort_by=created_at:desc&limit=40"

SYSTEM_INSTRUCTION = """
Ești un expert tehnic în echipamente video și videoproiectoare. 
Analizează titlul și descrierea dintr-un anunț OLX și stabilește dacă proiectorul îndeplinește STRICT următoarele criterii:

CRITERII TEHNICE:
1. Luminozitate: minim 3000 ANSI Lumens.
2. Rezoluție nativă: minim 1280x800 (WXGA). Nu accepta rezoluții inferioare (ex: 800x600).
3. Raport aspect nativ: 16:9 sau 16:10.
4. An fabricație / Lansare model: aproximativ 2020 sau mai nou.
5. Conectivitate: Port HDMI prezent.
6. Stare: În stare funcțională.

Dacă specificațiile nu sunt menționate detaliat, folosește-ți cunoștințele tehnice pentru a identifica fișa oficială a modelului.

Răspunde DOAR în format JSON valid cu următoarea structură:
{
  "eligible": true/false,
  "reject_reason": "Motivul respingerii (dacă nu e eligibil)",
  "brand_model": "Brand și Model",
  "specs": "Rezoluție | Lumeni | Aspect | An",
  "quality_score": "X/10 - Motivare scurtă preț/performanță",
  "summary": "Scurt rezumat despre stare"
}
"""

def init_gemini():
    if not GEMINI_API_KEY:
        raise ValueError("Lipsește variabila GEMINI_API_KEY!")
    return genai.Client(api_key=GEMINI_API_KEY)

def init_google_sheets():
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("Lipsește variabila GOOGLE_CREDENTIALS_JSON!")
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open(SPREADSHEET_NAME)
    worksheet = sh.sheet1
    
    if not worksheet.row_values(1):
        worksheet.append_row([
            "Data Inserării", "Titlu Anunț", "Brand & Model", 
            "Detalii Tehnice", "Preț", "Nota Calitate/Preț", 
            "Sumar / Stare", "Link Anunț"
        ])
    return worksheet

def fetch_olx_ads_api(session):
    print("Se extrag datele prin API-ul intern OLX...")
    response = session.get(OLX_API_URL)
    
    if response.status_code != 200:
        print(f"Eroare la accesare API: Status {response.status_code}")
        return []
        
    data = response.json()
    offers = data.get("data", [])
    ads = []
    
    for offer in offers:
        title = offer.get("title", "")
        url = offer.get("url", "")
        
        price_str = "Nespecificat"
        for param in offer.get("params", []):
            if param.get("key") == "price":
                val = param.get("value", {})
                price_str = f"{val.get('value', '')} {val.get('currency', 'RON')}"
                break
                
        raw_desc = offer.get("description", "")
        clean_desc = raw_desc.replace("<br />", "\n").replace("<br/>", "\n").replace("<p>", "").replace("</p>", "\n")
        
        ads.append({
            "title": title,
            "link": url,
            "price": price_str,
            "description": clean_desc
        })
        
    return ads

def analyze_ad_with_gemini(client, title, price, description):
    prompt = f"Titlu: {title}\nPreț: {price}\nDescriere:\n{description}"
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Eroare Gemini: {e}")
        return None

def main():
    print("Pornire verificare anunțuri noi (API Mode)...")
    
    gemini_client = init_gemini()
    worksheet = init_google_sheets()
    
    try:
        inserted_links = set(worksheet.col_values(8)[1:])
    except Exception:
        inserted_links = set()
    
    session = requests.Session(impersonate="chrome124")
    ads = fetch_olx_ads_api(session)
    print(f"S-au găsit {len(ads)} anunțuri recente pe OLX.")
    
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
            print(" -> ACCEPTAT! Se salvează în Google Sheet...")
            worksheet.append_row([
                today_str,
                ad["title"],
                analysis.get("brand_model", "N/A"),
                analysis.get("specs", "N/A"),
                ad["price"],
                analysis.get("quality_score", "N/A"),
                analysis.get("summary", "N/A"),
                ad["link"]
            ])
            inserted_links.add(ad["link"])
            inserted_count += 1
        else:
            reason = analysis.get("reject_reason", "Neeligibil")
            print(f" -> Respins: {reason}")
            
        time.sleep(1)

    print(f"\nFinalizat! Au fost adăugate {inserted_count} anunțuri noi conforme.")

if __name__ == "__main__":
    main()
