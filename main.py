import os
import json
import re
from datetime import datetime, timedelta
import requests
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

# URL-ul de căutare pe OLX (ordonați după cele mai recente)
OLX_SEARCH_URL = "https://www.olx.ro/d/electronice-electrocasnice/tv-audio-video/videoproiectoare/?search%5Border%5D=created_at%3Adesc"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# PROMPT GEMINI (Pentu filtrare & calculat scor)
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """
Ești un expert tehnic în echipamente video și videoproiectoare. 
Analizează titlul și descrierea furnizate din anunțul OLX pentru a determina dacă videoproiectorul respectă STRICT criteriile următoare:

CRITERII TEHNICE OBLIGATORII:
1. Luminozitate: minim 3000 ANSI Lumens (sau echivalent de producție verificat pentru modelul respectiv).
2. Rezoluție nativă: minim 1280x800 (WXGA, HD+, Full HD 1080p, 2K, 4K).
3. Raport aspect nativ: 16:9 sau 16:10 (nu 4:3).
4. An fabricație / Lansare model: aproximativ 2020 sau mai nou.
5. Conectivitate: Port HDMI prezent.
6. Stare: În stare perfectă/bună de funcționare (fără lămpi arse, pete pe imagine sau defecte majore).

Dacă specificațiile tehnice complete nu sunt scrise explicit în anunț, folosește-ți cunoștințele tehnice despre modelul identificat în titlu/text pentru a verifica specificațiile reale ale producătorului.

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
    """Inițializează clientul Gemini folosind noul SDK `google-genai`."""
    if not GEMINI_API_KEY:
        raise ValueError("Lipseste variabila de mediu GEMINI_API_KEY!")
    return genai.Client(api_key=GEMINI_API_KEY)

def init_google_sheets():
    """Autentificare în Google Sheets folosind contul de serviciu (Service Account)."""
    if not GOOGLE_CREDENTIALS_JSON:
        raise ValueError("Lipseste variabila de mediu GOOGLE_CREDENTIALS_JSON!")
    
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    gc = gspread.service_account_from_dict(creds_dict)
    
    # Deschide spreadsheet-ul și primul worksheet
    sh = gc.open(SPREADSHEET_NAME)
    worksheet = sh.sheet1
    
    # Verifică/Creează antetul dacă tabelul este gol
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
        col_links = worksheet.col_values(8)  # Coloana H este Link Anunț
        return set(col_links[1:])  # Fără antet
    except Exception as e:
        print(f"Avertisment la citirea linkurilor existente: {e}")
        return set()

def fetch_olx_ads():
    """Extrage lista de anunțuri recente de pe prima pagină OLX."""
    response = requests.get(OLX_SEARCH_URL, headers=HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    
    cards = soup.find_all("div", {"data-testid": "l-card"})
    ads = []
    
    for card in cards:
        # Preluare Titlu și Link
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

def fetch_ad_details(ad_url):
    """Extrage textul detaliat al descrierii unui anunț."""
    try:
        res = requests.get(ad_url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            return ""
        soup = BeautifulSoup(res.text, "html.parser")
        
        # OLX folosește div-uri de clasa "css-1m82329" sau data-description
        desc_div = soup.find("div", {"data-cy": "ad_description"}) or soup.find("div", class_=re.compile("description"))
        if desc_div:
            return desc_div.get_text(separator="\n", strip=True)
    except Exception as e:
        print(f"Eroare la preluarea descrierii pentru {ad_url}: {e}")
    return ""

def analyze_ad_with_gemini(client, title, price, description):
    """Analizează anunțul cu Gemini API folosind Structured JSON Output."""
    prompt = f"""
    Titlu Anunț: {title}
    Preț solicitat: {price}
    Descriere completă din anunț:
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
        data = json.loads(response.text)
        return data
    except Exception as e:
        print(f"Eroare la analiza Gemini: {e}")
        return None

def main():
    print("Pornește automatizarea verificării anunțurilor OLX...")
    
    gemini_client = init_gemini()
    worksheet = init_google_sheets()
    inserted_links = get_already_inserted_links(worksheet)
    
    ads = fetch_olx_ads()
    print(f"S-au găsit {len(ads)} anunțuri pe prima pagină OLX.")
    
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    inserted_count = 0
    
    for ad in ads:
        if ad["link"] in inserted_links:
            print(f"Sărit (deja salvat): {ad['title']}")
            continue
            
        print(f"\nProcesare: {ad['title']} ({ad['price']})")
        description = fetch_ad_details(ad["link"])
        
        analysis = analyze_ad_with_gemini(
            client=gemini_client,
            title=ad["title"],
            price=ad["price"],
            description=description
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

    print(f"\nFinalizat! S-au inserat {inserted_count} anunțuri noi eligibile.")

if __name__ == "__main__":
    main()
