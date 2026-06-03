import os
import re
import sys
import json
import argparse
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

# Default file path for the database
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")

def parse_existing_database(db_path):
    """
    Parses the existing data.js file and returns the list of laptops.
    """
    if not os.path.exists(db_path):
        print(f"Database file not found at {db_path}. Creating a new database.")
        return []
    
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Remove single-line JS comments
        content_no_comments = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        
        # Match array contents within window.laptops = [ ... ]
        match = re.search(r'window\.laptops\s*=\s*(\[.*\])\s*;?\s*$', content_no_comments, re.DOTALL)
        if not match:
            # Fallback: search for first [ and last ]
            match = re.search(r'(\[.*\])', content_no_comments, re.DOTALL)
        
        if match:
            json_str = match.group(1)
            # Remove trailing commas before closing brackets to prevent json parsing errors
            json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
            return json.loads(json_str)
        else:
            print("Could not locate window.laptops array in data.js. Initializing with empty list.")
            return []
    except Exception as e:
        print(f"Warning: Failed to parse existing database due to error: {e}. Starting fresh.")
        return []

def save_database(db_path, laptops):
    """
    Saves the list of laptops back to the data.js file using window.laptops format.
    Does not overwrite if the laptops list is empty (validation guard).
    """
    if not laptops:
        print("Warning: Scraped laptops list is empty. Aborting write to prevent site crash. Old database preserved.")
        return False
        
    try:
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        
        # Generate exact format content
        content = f"window.laptops = {json.dumps(laptops, indent=2, ensure_ascii=False)};\n"
        
        with open(db_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        print(f"Database saved successfully to {db_path}. Total records: {len(laptops)}")
        return True
    except Exception as e:
        print(f"Error saving database: {e}")
        return False

def reset_database(db_path):
    """
    Overwrites the database file to initialize it with an empty array.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        content = "window.laptops = [];\n"
        with open(db_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Database reset successfully at {db_path}.")
        return True
    except Exception as e:
        print(f"Error resetting database: {e}")
        return False

def scrape_product_page(url):
    """
    Uses Playwright to scrape the title and full visible text content of the product URL.
    """
    print(f"Launching Playwright to scrape: {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # Navigate to url and wait until network is idle
            page.goto(url, wait_until="networkidle", timeout=60000)
            
            title = page.title()
            body_text = page.locator("body").inner_text()
            
            # Clean up double line breaks and excess whitespaces to optimize context length
            body_text = re.sub(r'\n+', '\n', body_text)
            body_text = re.sub(r' +', ' ', body_text)
            
            browser.close()
            return title, body_text
    except Exception as e:
        print(f"Error during scraping: {e}")
        sys.exit(1)

def extract_device_json(title, text_content, api_key):
    """
    Uses Google Gemini API (gemini-2.5-flash) to parse text content into a strict, validated JSON schema.
    """
    print("Calling Google Gemini to structure product data...")
    if not api_key:
        print("Error: Gemini API key is missing. Set the GEMINI_API_KEY environment variable.")
        sys.exit(1)
        
    system_prompt = """You are a Senior Full-Stack Engineer and Data Architect for "TechDekho".
Extract all hardware specs and review summaries from the provided text about a device (laptop, phone, or tablet) and format it into a STRICT JSON object.

You MUST return a JSON object with exactly these top-level keys:
- 'id' (string, e.g., 'apple-macbook-air-m3')
- 'category' (string)
- 'brand' (string)
- 'name' (string)
- 'price' (number)
- 'budget' (string)
- 'os' (array of strings)
- 'useCases' (array of strings)
- 'strengths' (array of strings)
- 'tags' (object with keys: 'cpu', 'gpu', 'ram', 'display', 'storage')
- 'why' (string)
- 'specs' (object with keys: 'processor', 'ram', 'storage', 'screen')
- 'realWorldBattery' (string)
- 'userSentimentSummary' (string)
- 'hiddenFlaws' (string)
- 'pros' (array of strings)
- 'cons' (array of strings)

You MUST populate EVERY field. Use the fallback rules defined in the schema if information is missing.

STRICT SCHEMA DEFINITION AND RULES:
{
  "id": "lowercase-hyphenated-slug-for-device", // e.g. "asus-rog-zephyrus-g14-2024" or "iphone-15-pro"
  "category": "laptops", // must be exactly one of: "laptops", "phones", "tablets"
  "brand": "BrandName", // e.g. "Apple", "ASUS", "Lenovo", "Samsung", "OnePlus", "HP", "Dell"
  "name": "Full Product Name", // e.g. "ASUS ROG Zephyrus G14 (2024)"
  "price": 109999, // integer price in INR (if given in USD/EUR, convert logically using 1 USD = 83 INR, and estimate average street price)
  "budget": "budget100150", // Choose EXACTLY one based on price in INR:
                            // < 40k: "budgetUnder40"
                            // 40k-60k: "budget4060"
                            // 60k-80k: "budget6080"
                            // 80k-100k: "budget80100"
                            // 100k-150k: "budget100150"
                            // 150k-200k: "budget150200"
                            // > 200k: "budgetOver200"
  "os": ["windows"], // array of strings, must contain at least one of: "windows", "mac", "android", "ios", "linux"
  "useCases": ["coding"], // array of strings (ONLY for category="laptops", choose from: "coding", "student-eng", "gaming", "creative", "general"). For phones/tablets, use empty array []
  "strengths": ["power", "display"], // array of strings, choose from: "battery", "portability", "power", "value", "display", "build"
  "tags": {
    "cpu": "intel", // must be exactly one of: "apple", "intel", "amd", "snapdragon", "mediatek", "exynos"
    "gpu": "nvidia", // must be exactly one of: "integrated", "nvidia", "amd", "apple"
    "ram": "16", // string containing integer value of RAM in GB, e.g. "8", "12", "16", "24", "32", "64"
    "display": "oled", // must be exactly one of: "ips", "oled", "retina", "amoled"
    "storage": "512" // string containing integer value of storage in GB, e.g. "128", "256", "512", "1024", "2048"
  },
  "why": "A compelling 1-sentence summary of why this device is a great match for its target audience.",
  "specs": {
    "processor": "Specific processor details", // e.g. "Intel Core Ultra 7 155H"
    "ram": "Specific RAM specifications", // e.g. "16GB LPDDR5X Dual Channel"
    "storage": "Specific storage specifications", // e.g. "1TB PCIe Gen4 NVMe M.2 SSD"
    "screen": "Specific screen specifications" // e.g. "14-inch 2.8K (2880 x 1800) OLED 120Hz"
  },
  "realWorldBattery": "e.g. 8.5 hours of mixed productivity use.", // description of realistic battery longevity
  "userSentimentSummary": "A concise summary of user reviews, capturing the general sentiment and key features.",
  "hiddenFlaws": "A description of structural flaws, thermal behavior under load, soldered memory, or upgrade limits.",
  "pros": [
    "Pro 1",
    "Pro 2",
    "Pro 3" // Must contain EXACTLY 3 pros (strings)
  ],
  "cons": [
    "Con 1",
    "Con 2",
    "Con 3" // Must contain EXACTLY 3 cons (strings)
  ],
  "headphoneJack": true, // boolean (does it have a 3.5mm jack?)
  "sdCard": false, // boolean (does it have an SD/MicroSD card slot?)
  "chargerInBox": true, // boolean (does a charger ship in the retail packaging?)
  "color": "silver" // must be exactly one of: "white", "vibrant", "black", "silver"
}
"""
    
    user_prompt = f"Scraped Title: {title}\n\nScraped Text Content:\n{text_content}"
    
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                system_instruction=system_prompt,
            ),
        )
        parsed_json = json.loads(response.text)
        
        # Defensive schema alignment checks on fields
        required_keys = [
            "id", "category", "brand", "name", "price", "budget", "os", "useCases", 
            "strengths", "tags", "why", "specs", "realWorldBattery", "userSentimentSummary", 
            "hiddenFlaws", "pros", "cons", "headphoneJack", "sdCard", "chargerInBox", "color"
        ]
        
        # Populate defaults for missing keys to ensure absolute zero frontend crash risk
        for key in required_keys:
            if key not in parsed_json:
                if key in ["os", "useCases", "strengths", "pros", "cons"]:
                    parsed_json[key] = []
                elif key in ["headphoneJack", "sdCard", "chargerInBox"]:
                    parsed_json[key] = True
                elif key == "price":
                    parsed_json[key] = 0
                elif key == "tags":
                    parsed_json[key] = {"cpu": "intel", "gpu": "integrated", "ram": "8", "display": "ips", "storage": "256"}
                elif key == "specs":
                    parsed_json[key] = {"processor": "N/A", "ram": "N/A", "storage": "N/A", "screen": "N/A"}
                else:
                    parsed_json[key] = "N/A"
                    
        # Sanitize list lengths
        for key in ["pros", "cons"]:
            if len(parsed_json[key]) < 3:
                parsed_json[key].extend(["Benefit/Issue not specified"] * (3 - len(parsed_json[key])))
            parsed_json[key] = parsed_json[key][:3]
            
        return parsed_json
    except Exception as e:
        print(f"Error parsing with Gemini API: {e}")
        sys.exit(1)

def get_product_links(category_url):
    """
    Takes a base category URL, uses Playwright to load the page, and uses BeautifulSoup
    to extract all valid product links matching a regex filter.
    """
    print(f"Opening category page to extract product links: {category_url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto(category_url, wait_until="networkidle", timeout=60000)
            html_content = page.content()
            browser.close()
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Find all absolute and relative links on the page
            raw_links = []
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                full_url = urljoin(category_url, href)
                raw_links.append(full_url)
                
            # Filter links using a regex pattern
            product_regex = re.compile(
                r'(review|product|laptop|phone|tablet|spec|hardware|notebookcheck\.net\/[a-zA-Z0-9\-]+\.[0-9]+\.0\.html)',
                re.IGNORECASE
            )
            
            product_links = set()
            for link in raw_links:
                # Remove fragment identifiers and queries
                clean_link = link.split('#')[0].split('?')[0]
                if product_regex.search(clean_link) and clean_link != category_url:
                    product_links.add(clean_link)
                    
            return list(product_links)
    except Exception as e:
        print(f"Error extracting product links from {category_url}: {e}")
        return []

def main():
    parser = argparse.ArgumentParser(description="TechDekho Automated Production Data Pipeline Scraper and Recursive Crawler (Full Refresh)")
    parser.add_argument("--categories", nargs="+", help="Space-separated list of category URLs to crawl")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Path to data.js database file")
    parser.add_argument("--reset", action="store_true", help="Reset the database to an empty array and exit")
    args = parser.parse_args()
    
    # 0. Handle reset command if triggered
    if args.reset:
        reset_database(args.db)
        sys.exit(0)
        
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("CRITICAL: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)
        
    # Predefined list of base category URLs
    base_category_urls = args.categories if args.categories else [
        "https://www.notebookcheck.net/Reviews.55.0.html"
    ]
    
    print(f"Pipeline running (FULL REFRESH MODE). DB target: {args.db}")
    print(f"Base Category URLs to crawl: {base_category_urls}")
    
    # 1. Iterate through each category to extract product links
    collected_product_links = set()
    for cat_url in base_category_urls:
        links = get_product_links(cat_url)
        print(f"Category {cat_url} produced {len(links)} product links.")
        for link in links:
            collected_product_links.add(link)
            
    print(f"Total unique product links collected to process: {len(collected_product_links)}")
    
    # 2. Initialize seen_ids set for duplicate prevention and empty list for clean crawling data
    seen_ids = set()
    all_scraped_data = []
    
    # 3. Loop through the collected product links, perform the scraping, call Gemini for structuring
    for index, url in enumerate(collected_product_links, 1):
        print(f"\nProcessing product link [{index}/{len(collected_product_links)}]: {url}")
        try:
            # Scrape page
            title, text_content = scrape_product_page(url)
            
            # Call Gemini and structure data
            new_device = extract_device_json(title, text_content, api_key)
            
            device_id = new_device.get("id")
            # Duplicate prevention check in the main loop
            if device_id in seen_ids:
                print(f"Device with ID '{device_id}' already seen in this run. Skipping duplicate.")
                continue
                
            seen_ids.add(device_id)
            all_scraped_data.append(new_device)
            print(f"Extracted device: {new_device['brand']} {new_device['name']} (ID: {device_id})")
            
        except Exception as e:
            print(f"Error processing URL {url}: {e}")
            continue
            
    # 4. Save the refreshed list and completely overwrite data.js (includes empty checks)
    save_database(args.db, all_scraped_data)
    print("Full Refresh Pipeline run completed successfully!")

if __name__ == "__main__":
    main()