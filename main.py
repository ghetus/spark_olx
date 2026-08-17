import os
import json
import time
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

OLX_SEARCH_URL = "https://www.olx.ro/d/electronice-electrocasnice/tv-audio-video/videoproiectoare/?search%5Border%5D=created_at%3Adesc"

SYSTEM_INSTRUCTION = """
Ești un expert tehnic în echipamente video și videoproiectoare. 
Analizează titlul și descrierea dintr-un anunț OLX și stabilește dacă proiectorul îndeplinește STRICT următoarele criterii:

CRITERII TEHNICE:
1. Luminozitate: minim 3000 ANSI Lumens.
2. Rezoluție nativă: minim 1280x800 (WXGA). Nu accepta rezoluții inferioare.
3. Raport aspect nativ: 16:9 sau 16:10.
4. An fabricație / Lansare model: aproximativ 2020 sau mai nou.
5. Conectivitate: Port HDMI prezent.
6. Stare: În stare funcțională.

Dacă specificațiile nu sunt menționate detaliat în anunț, verifică modelul conform fișei oficiale a producătorului.

Răspunde DOAR în format JSON valid cu următoarea structură:
{
  "eligible": true/false,
  "reject_reason": "Motivul respingerii",
  "brand_model": "Brand și Model",
  "specs": "Rezoluție | Lumeni | Aspect | An",
  "quality_score": "X/10 - Motivare scurtă",
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
        return set()

def fetch_olx_ads(session):
    print("Se accesează OLX (simulare Chrome)...")
    response = session.get(OLX_SEARCH_URL)
    
    if response.status_code != 200:
        print(f"Eroare la accesare OLX: Status {response.status_code}")
        return []
    
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.find_all("div", {"data-testid": "l-card"})
    
    ads = []
    for card in cards:
        title_elem = card.find("h6")
        link_elem = card.find("a")
        price_elem = card.find("p", {"data-testid": "ad-price"})
        
        if not title_elem or not link_elem:
            continue
            
        title = title_elem.get_text(strip=True)
        raw_href = link_elem.get("href", "")
        link = raw_href if raw_href.startswith("http") else f"https://www.olx.ro{raw_href}"
        price = price_elem.get_text(strip=True) if price_elem else "Nespecificat"
        
        ads.append({
            "title": title,
            "link": link,
            "price": price
        })
        
    return ads

def fetch_ad_description(session, ad_url):
    try:
        res = session.get(ad_url)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            desc_div = soup.find("div", {"data-cy": "ad_description"})
            if desc_div:
                return desc_div.get_text(separator="\n", strip=True)
    except Exception:
        pass
    return ""

def analyze_ad_with_gemini(client, title, price, description):
    prompt = f"Titlu: {title}\nPreț: {price}\nDescriere:\n{description}"
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
    print("Pornire verificare anunțuri noi...")
    
    gemini_client = init_gemini()
    worksheet = init_google_sheets()
    inserted_links = get_already_inserted_links(worksheet)
    
    # Folosim impersonate pentru a ocoli filtrul anti-bot
    session = requests.Session(impersonate="chrome124")
    ads = fetch_olx_ads(session)
    print(f"S-au extras {len(ads)} anunțuri recente.")
    
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    inserted_count = 0
    
    for ad in ads:
        if ad["link"] in inserted_links:
            continue
            
        print(f"\nAnalizare: {ad['title']} ({ad['price']})")
        description = fetch_ad_description(session, ad["link"])
        
        analysis = analyze_ad_with_gemini(
            client=gemini_client,
            title=ad["title"],
            price=ad["price"],
            description=description
        )
        
        if not analysis:
            continue
            
        if analysis.get("eligible") is True:
            print(" -> ACCEPTAT! Se salvează în tabel...")
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
            
        time.sleep(1)

    print(f"\nFinalizat! Au fost adăugate {inserted_count} anunțuri noi conforme.")

if __name__ == "__main__":
    main()
