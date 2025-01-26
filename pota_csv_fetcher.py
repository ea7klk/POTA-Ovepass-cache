import requests
import json
import logging
from datetime import datetime, timedelta
import os
import time
import csv
from io import StringIO

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

POTA_DATA_FILE = "pota_data.json"
LAST_FETCH_FILE = "last_fetch_time.txt"
FETCH_INTERVAL = timedelta(hours=1)
CSV_URL = "https://pota.app/all_parks_ext.csv"

def should_fetch_data():
    if not os.path.exists(LAST_FETCH_FILE):
        return True
    
    try:
        with open(LAST_FETCH_FILE, 'r') as f:
            last_fetch_str = f.read().strip()
            last_fetch = datetime.fromisoformat(last_fetch_str)
            return datetime.now() - last_fetch >= FETCH_INTERVAL
    except Exception as e:
        logger.error(f"Error reading last fetch time: {e}")
        return True

def update_last_fetch_time():
    try:
        with open(LAST_FETCH_FILE, 'w') as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        logger.error(f"Error writing last fetch time: {e}")

def fetch_and_parse_csv(force=False):
    """Fetch and parse the POTA CSV file."""
    try:
        # Fetch CSV data
        response = requests.get(CSV_URL)
        response.raise_for_status()
        response.encoding = 'utf-8'  # Ensure proper character encoding
        
        # Parse CSV
        elements = []
        csv_data = StringIO(response.text)
        reader = csv.reader(csv_data)
        next(reader)  # Skip header row
        
        for row in reader:
            try:
                # Extract fields
                pota_ref = row[0]
                name = row[1]
                active = row[2]
                
                # Skip inactive parks
                if active != '1':
                    logger.debug(f"Skipping inactive park {pota_ref}")
                    continue
                
                # Skip records without coordinates
                if not row[5] or not row[6] or row[5] == '' or row[6] == '':
                    logger.debug(f"Skipping park {pota_ref} due to missing coordinates")
                    continue
                
                try:
                    lat = float(row[5])  # latitude
                    lon = float(row[6])  # longitude
                except ValueError:
                    logger.debug(f"Skipping park {pota_ref} due to invalid coordinates: lat={row[5]}, lon={row[6]}")
                    continue
                
                # Create Overpass-format element with explicit active status
                element = {
                    'type': 'node',
                    'lat': lat,
                    'lon': lon,
                    'tags': {
                        'communication:amateur_radio:pota': pota_ref,
                        'name': name,
                        'pota:active': '1',
                        'unmapped_osm': 'true'
                    }
                }
                elements.append(element)
                logger.debug(f"Added active park {pota_ref}: {name}")
                
            except (IndexError, ValueError) as e:
                logger.debug(f"Skipping invalid row: {e}")
                continue
        
        result = {'elements': elements, 'version': 0.6, 'generator': 'POTA CSV Parser'}
        logger.info(f"Successfully processed {len(elements)} active parks from CSV")
        
        # Save data if not forced refresh
        if not force:
            save_data(result)
            update_last_fetch_time()
        
        return result
        
    except Exception as e:
        logger.error(f"Error fetching or parsing CSV: {e}")
        return None

def save_data(data):
    """Save the fetched data to a file."""
    try:
        with open(POTA_DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info(f"Successfully saved data to {POTA_DATA_FILE}")
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def load_data():
    """Load data from the cached file."""
    try:
        if os.path.exists(POTA_DATA_FILE):
            with open(POTA_DATA_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading cached data: {e}")
    return None

def update_pota_data(force=False):
    """Main function to update POTA data."""
    if force or should_fetch_data():
        logger.info("Fetching new POTA data...")
        data = fetch_and_parse_csv(force)
        
        if not data:
            logger.warning("Failed to fetch new data, trying to load cached data")
            return load_data()
        
        return data
    else:
        logger.info("Using cached POTA data (less than 1 hour old)")
        return load_data()

if __name__ == '__main__':
    update_pota_data()