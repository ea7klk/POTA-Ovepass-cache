import requests
import json
import logging
import os
import hashlib
from flask import Flask, request, Response, jsonify
from flask_cors import CORS
from flask_compress import Compress
import schedule
import time
from datetime import datetime
from threading import Thread, Lock
import urllib.parse
from pota_csv_fetcher import update_pota_data

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configure Flask-Compress with Brotli compression
app.config['COMPRESS_ALGORITHM'] = 'br'  # Use Brotli compression
app.config['COMPRESS_BR_LEVEL'] = 4      # Balanced Brotli compression level for dynamic content
Compress(app)

# Global variables to store the cached data and metadata
cached_data = None
last_cache_update = None
cache_refresh_count = 0
cache_hit_count = 0
schedule_thread = None
cache_lock = Lock()
bbox_cache = {}
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "900"))
MAX_CACHE_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "32"))

HTTP_HEADERS = {
    "User-Agent": "pota-overpass-cache/1.1 (+https://github.com/ea7klk/pota-ovepass-cache)",
    "Accept": "application/json",
}

OVERPASS_URLS = [
    os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CACHE_DIR = os.getenv("CACHE_DIR", "/app/cache")

def merge_pota_data(overpass_data, pota_data, bbox=None):
    """Merge POTA data with Overpass data, preserving names from POTA CSV data."""
    if not pota_data or 'elements' not in pota_data or not pota_data['elements']:
        return overpass_data

    if 'elements' not in overpass_data:
        overpass_data['elements'] = []

    # Create a mapping of POTA references to their CSV names
    pota_names = {}
    pota_refs = set()
    for element in pota_data['elements']:
        if 'tags' in element and 'communication:amateur_radio:pota' in element['tags']:
            pota_ref = element['tags']['communication:amateur_radio:pota']
            pota_refs.add(pota_ref)
            if 'name' in element['tags']:
                pota_names[pota_ref] = element['tags']['name']

    # Filter Overpass elements to keep only those with a pota_ref in POTA data
    filtered_elements = []
    for element in overpass_data['elements']:
        if 'tags' in element and 'communication:amateur_radio:pota' in element['tags']:
            pota_ref = element['tags']['communication:amateur_radio:pota']
            if pota_ref in pota_refs:
                # Update name if pota_ref is found in POTA data
                if pota_ref in pota_names:
                    element['tags']['name'] = pota_names[pota_ref]
                filtered_elements.append(element)

    overpass_data['elements'] = filtered_elements

    # Add POTA elements that don't exist in Overpass data
    overpass_refs = {element['tags']['communication:amateur_radio:pota'] for element in overpass_data['elements'] 
                     if 'tags' in element and 'communication:amateur_radio:pota' in element['tags']}

    for element in pota_data['elements']:
        if 'tags' in element and 'communication:amateur_radio:pota' in element['tags']:
            pota_ref = element['tags']['communication:amateur_radio:pota']
            if bbox is not None:
                south, west, north, east = bbox
                lat, lon = element.get('lat'), element.get('lon')
                if lat is None or lon is None or not (south <= lat <= north and west <= lon <= east):
                    continue
            if pota_ref not in overpass_refs:
                overpass_data['elements'].append(element)

    return overpass_data


def normalize_bbox(bbox):
    return tuple(round(value, 4) for value in bbox)


def cache_file(cache_key):
    key = hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"bbox-{key}.json")


def load_disk_cache(cache_key):
    try:
        with open(cache_file(cache_key), "r", encoding="utf-8") as cache_handle:
            cached_entry = json.load(cache_handle)
        cached_at = float(cached_entry["cached_at"])
        if time.time() - cached_at < CACHE_TTL_SECONDS:
            return cached_at, cached_entry["data"]
        os.remove(cache_file(cache_key))
    except (FileNotFoundError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None
    return None


def save_disk_cache(cache_key, cached_at, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        target = cache_file(cache_key)
        temporary = f"{target}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as cache_handle:
            json.dump({"cached_at": cached_at, "data": data}, cache_handle)
        os.replace(temporary, target)
    except OSError as error:
        logger.warning(f"Unable to persist cache entry: {error}")


def fetch_overpass_data(bbox=None):
    global cached_data, last_cache_update, cache_refresh_count, cache_hit_count
    overpass_url = "https://overpass-api.de/api/interpreter"
    if bbox is None:
        overpass_query = """
        [out:json][timeout:60];
        (
          nwr["communication:amateur_radio:pota"];
        );
        out geom;
        """
    else:
        south, west, north, east = bbox
        overpass_query = f"""
        [out:json][timeout:60];
        (
          nwr["communication:amateur_radio:pota"]({south},{west},{north},{east});
        );
        out geom;
        """
    
    cache_key = normalize_bbox(bbox) if bbox is not None else None

    with cache_lock:
        if cache_key is not None:
            cached_entry = bbox_cache.get(cache_key)
            if cached_entry is not None:
                cached_at, cached_value = cached_entry
                if time.time() - cached_at < CACHE_TTL_SECONDS:
                    cached_data = cached_value
                    last_cache_update = cached_at
                    cache_hit_count += 1
                    logger.info(f"Cache hit for bbox {cache_key} (hit #{cache_hit_count})")
                    return cached_value
                del bbox_cache[cache_key]
            else:
                disk_entry = load_disk_cache(cache_key)
                if disk_entry is not None:
                    cached_at, cached_value = disk_entry
                    bbox_cache[cache_key] = (cached_at, cached_value)
                    cached_data = cached_value
                    last_cache_update = cached_at
                    cache_hit_count += 1
                    logger.info(f"Persistent cache hit for bbox {cache_key} (hit #{cache_hit_count})")
                    return cached_value

        try:
            start_time = time.time()
            overpass_data = None
            last_error = None
            for overpass_url in OVERPASS_URLS:
                try:
                    response = requests.get(
                        overpass_url,
                        params={'data': overpass_query},
                        headers=HTTP_HEADERS,
                        timeout=(10, 120),
                    )
                    response.raise_for_status()
                    overpass_data = response.json()
                    logger.info(f"Fetched Overpass data from {overpass_url}")
                    break
                except (requests.RequestException, ValueError) as error:
                    last_error = error
                    logger.warning(f"Overpass backend failed ({overpass_url}): {error}")

            if overpass_data is None:
                raise requests.RequestException(f"all Overpass backends failed: {last_error}")
            
            # Fetch POTA data and merge with Overpass data
            pota_data = update_pota_data()
            cached_data = merge_pota_data(overpass_data, pota_data, bbox)
            
            last_cache_update = time.time()
            cache_refresh_count += 1
            processing_time = last_cache_update - start_time
            logger.info(f"Cache refreshed (#{cache_refresh_count}). Total elements: {len(cached_data['elements'])}. "
                        f"Cache updated at: {time.ctime(last_cache_update)}. Processing time: {processing_time:.2f} seconds")
            if cache_key is not None:
                bbox_cache[cache_key] = (last_cache_update, cached_data)
                save_disk_cache(cache_key, last_cache_update, cached_data)
                while len(bbox_cache) > MAX_CACHE_ENTRIES:
                    oldest_key = min(bbox_cache, key=lambda key: bbox_cache[key][0])
                    del bbox_cache[oldest_key]
            return cached_data
        except requests.RequestException as e:
            logger.error(f"Failed to fetch data: {str(e)}")
            return None

def add_pota_tag_to_subelements(element):
    pota_value = element['tags'].get('communication:amateur_radio:pota', 'yes')
    
    if element['type'] == 'way':
        if 'geometry' in element:
            for node in element['geometry']:
                if 'tags' not in node:
                    node['tags'] = {}
                node['tags']['communication:amateur_radio:pota'] = pota_value
    elif element['type'] == 'relation':
        if 'members' in element:
            for member in element['members']:
                if member['type'] == 'way':
                    if 'tags' not in member:
                        member['tags'] = {}
                    member['tags']['communication:amateur_radio:pota'] = pota_value
    return element

def filter_data(south, west, north, east, data=None):
    with cache_lock:
        data_to_filter = data if data is not None else cached_data
        if data_to_filter is None:
            logger.warning("No cached data available")
            return None
        
        filtered_elements = []
        for element in data_to_filter['elements']:
            if 'type' in element:
                if element['type'] == 'node':
                    lat, lon = element.get('lat'), element.get('lon')
                    if lat is not None and lon is not None:
                        if south <= lat <= north and west <= lon <= east:
                            filtered_elements.append(element)
                elif element['type'] in ['way', 'relation']:
                    if 'bounds' in element:
                        bounds = element['bounds']
                        if (south <= bounds['minlat'] <= north or south <= bounds['maxlat'] <= north) and \
                           (west <= bounds['minlon'] <= east or west <= bounds['maxlon'] <= east):
                            filtered_elements.append(add_pota_tag_to_subelements(element))
                    elif 'geometry' in element:
                        for point in element['geometry']:
                            lat, lon = point.get('lat'), point.get('lon')
                            if lat is not None and lon is not None:
                                if south <= lat <= north and west <= lon <= east:
                                    filtered_elements.append(add_pota_tag_to_subelements(element))
                                    break
        
        logger.info(f"Filtered {len(filtered_elements)} elements out of {len(data_to_filter['elements'])}")
        return {'elements': filtered_elements, 'version': 0.6, 'generator': 'Overpass API POTA Cache'}

def parse_query(query):
    try:
        # Check if the query is URL-encoded
        if '%' in query:
            query = urllib.parse.unquote(query)
        
        # Extract bounding box from the query
        bbox = query.split('(')[1].split(')')[0].split(',')
        south, west, north, east = map(float, bbox)
        logger.info(f"Extracted bounding box: {south}, {west}, {north}, {east}")
        return south, west, north, east
    except (IndexError, ValueError) as e:
        logger.error(f"Invalid query format: {str(e)}")
        return None

@app.route('/api/interpreter', methods=['GET', 'POST'])
@app.route('/api/overpass', methods=['GET', 'POST'])
def query_data():
    start_time = time.time()
    if request.method == 'GET':
        query = request.args.get('data') or request.args.get('query')
    else:  # POST
        query = request.form.get('data') or request.form.get('query')

    logger.info(f"Received query: {query}")

    if not query:
        logger.warning("Missing query data")
        return Response("Missing query data", status=400)

    bbox = parse_query(query)
    if bbox is None:
        return Response("Invalid query format", status=400)

    south, west, north, east = bbox
    overpass_data = fetch_overpass_data(bbox)
    if overpass_data is None:
        logger.error("Unable to refresh data for requested bounding box")
        return Response("Unable to refresh data", status=503)

    filtered_data = filter_data(south, west, north, east, overpass_data)

    processing_time = time.time() - start_time
    logger.info(f"Returning {len(filtered_data['elements'])} elements from cache. "
                f"Last cache update: {time.ctime(last_cache_update)}. "
                f"Processing time: {processing_time:.2f} seconds")
    
    return Response(json.dumps(filtered_data), mimetype='application/json')

@app.route('/reload2024', methods=['GET'])
def force_reload():
    """Force reload of POTA data."""
    try:
        data = update_pota_data(force=True)
        if data:
            return jsonify({
                "status": "success",
                "message": f"POTA data reloaded with {len(data['elements'])} elements",
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "status": "error",
                "message": "Failed to reload POTA data",
                "timestamp": datetime.now().isoformat()
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/cache_status', methods=['GET'])
def cache_status():
    with cache_lock:
        if cached_data is None:
            return jsonify({
                "status": "No data cached",
                "elements_count": 0,
                "last_update": None,
                "cache_refresh_count": cache_refresh_count,
                "cache_hit_count": cache_hit_count,
                "cache_entries": len(bbox_cache),
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
            })
        
        return jsonify({
            "status": "Cache available",
            "elements_count": len(cached_data['elements']),
            "last_update": time.ctime(last_cache_update),
            "cache_refresh_count": cache_refresh_count,
            "cache_hit_count": cache_hit_count,
            "cache_entries": len(bbox_cache),
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
        })

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "pota-overpass-cache",
        "status": "ok",
        "cache": "available" if cached_data is not None else "empty",
    })

@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({"status": "ok"})

def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(1)

def start_scheduler():
    global schedule_thread
    # Evict expired regional entries; POTA reference data is refreshed lazily.
    schedule.every(5).minutes.do(prune_cache)

    # Create and start the scheduler thread if it's not already running
    if schedule_thread is None or not schedule_thread.is_alive():
        schedule_thread = Thread(target=run_schedule)
        schedule_thread.daemon = True
        schedule_thread.start()


def prune_cache():
    now = time.time()
    with cache_lock:
        expired_keys = [
            key for key, (cached_at, _) in bbox_cache.items()
            if now - cached_at >= CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            del bbox_cache[key]

# Start the scheduler
start_scheduler()

if __name__ == '__main__':
    # Run the Flask app
    app.run(debug=True, host='0.0.0.0', port=5005)
