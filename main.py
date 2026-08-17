import os
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from curl_cffi import requests
from bs4 import BeautifulSoup
import gspread
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# CONFIGURARE
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Proiectoare OLX")

# Feed RSS nativ OLX - fără protecție antibot, oferă cele mai recente anunțuri
OLX_RSS_URL = "https://www.olx.ro/d/electronice-electrocasnice/tv-audio-video/videoproiectoare/q-videoproiector/?search%5Border%5D=created_at%3Adesc&format=rss"

SYSTEM_INSTRUCTION = """
Ești un expert tehnic în echipamente video și videoproiectoare. 
Analizează titlul și descrierea dintr-un anunț OLX și stabilește dacă proiectorul îndeplinește STRICT următoarele criterii:

CRITERII TEHNICE:
1. Luminozitate: minim 3000 ANSI Lumens.
2. Rezoluție nativă: minim 1280x800 (WXGA, HD+, Full HD 1080p, 2K, 4K).
3. Raport aspect nativ: 16:9 sau 16:10 (nu 4:3).
4. An fabricație / Lansare model: aproximativ 2020 sau mai nou.
5. Conectivitate: Port HDMI prezent.
6. Stare: În stare perfectă/bună de funcționare (fără lămpi consumate sau defecte majore).

Dacă specificațiile nu sunt menționate detaliat în anunț, verifică modelul conform fișei oficiale a producătorului.

Răspunde DOAR în format JSON valid cu următoarea structură:
{
  "eligible": true/false,
  "reject_reason": "Motivul respingerii",
  "brand_model": "Brand și Model",
  "specs": "Rezoluție | Lumeni | Aspect | An",
  "quality_score": "X/10 - Motivare scurtă preț vs performanță",
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
    try:
        col_links = worksheet.col_values(8)
        return set(col_links[1:])
    except Exception as e:
        print(f"Avertisment link-uri existente: {e}")
        return set()

def fetch_olx_ads(session):
    print("Preluare anunțuri prin Feed RSS OLX...")
    response = session.get(OLX_RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    
    if response.status_code != 200:
        print(f"Eroare RSS: {response.status_code}")
        return []

    root = ET.fromstring(response.content)
    ads = []

    for item in root.findall(".//item"):
        title = item.find("title").text if item.find("title") is not None else ""
        link = item.find("link").text if item.find("link") is not None else ""
        desc_raw = item.find("description").text if item.find("description") is not None else ""
        
        # Curățare text HTML din descrierea RSS
        soup = BeautifulSoup(desc_raw, "html.parser")
        desc_clean = soup.get_text(separator=" ", strip=True)
        
        # Extragere preț estimat din descriere / titlu dacă există
        price = "Verifică în anunț"
        if "Pret:" in desc_clean or "Preț:" in desc_clean:
            parts = desc_clean.split("Pret:") if "Pret:" in desc_clean else desc_clean.split("Preț:")
            if len(parts) > 1:
                price = parts[1].split("-")[0].strip()

        ads.append({
            "title": title,
            "link": link,
            "price": price,
            "description": desc_clean
        })

    return ads

def analyze_ad_with_gemini(client, title, price, description):
    prompt = f"""
    Titlu: {title}
    Preț estimat: {price}
    Descriere anunț:
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
        print(f"Eroare Gemini: {e}")
        return None

def main():
    print("Pornire verificare zilnică proiectoare...")
    
    gemini_client = init_gemini()
    worksheet = init_google_sheets()
    inserted_links = get_already_inserted_links(worksheet)
    
    session = requests.Session(impersonate="chrome124")
    ads = fetch_olx_ads(session)
    print(f"S-au recepționat {len(ads)} anunțuri recente din feed.")
    
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    inserted_count = 0
    
    for ad in ads:
        if ad["link"] in inserted_links:
            continue
            
        print(f"\nAnalizare: {ad['title']}")
        analysis = analyze_ad_with_gemini(
            client=gemini_client,
            title=ad["title"],
            price=ad["price"],
            description=ad["description"]
        )
        
        if not analysis:
            continue
            
        if analysis.get("eligible") is True:
            print(" -> PROIECTOR ELIGIBIL! Se adaugă în Google Sheet...")
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
            print(f" -> Respins: {analysis.get('reject_reason', 'Neconform')}")
            
        time.sleep(1)

    print(f"\nFinalizat! S-au adăugat {inserted_count} anunțuri noi în tabel.")

if __name__ == "__main__":
    main()
