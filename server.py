import requests
import json
import logging
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
schedule_thread = None
cache_lock = Lock()

HTTP_HEADERS = {
    "User-Agent": "pota-overpass-cache/1.1 (+https://github.com/ea7klk/pota-ovepass-cache)",
    "Accept": "application/json",
}

def merge_pota_data(overpass_data, pota_data):
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
            if pota_ref not in overpass_refs:
                overpass_data['elements'].append(element)

    return overpass_data


def fetch_overpass_data(bbox=None):
    global cached_data, last_cache_update, cache_refresh_count
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
    
    with cache_lock:
        try:
            start_time = time.time()
            # Fetch Overpass API data
            response = requests.get(
                overpass_url,
                params={'data': overpass_query},
                headers=HTTP_HEADERS,
                timeout=180,
            )
            response.raise_for_status()
            overpass_data = response.json()
            
            # Fetch POTA data and merge with Overpass data
            pota_data = update_pota_data()
            cached_data = merge_pota_data(overpass_data, pota_data)
            
            last_cache_update = time.time()
            cache_refresh_count += 1
            processing_time = last_cache_update - start_time
            logger.info(f"Cache refreshed (#{cache_refresh_count}). Total elements: {len(cached_data['elements'])}. "
                        f"Cache updated at: {time.ctime(last_cache_update)}. Processing time: {processing_time:.2f} seconds")
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
                "cache_refresh_count": cache_refresh_count
            })
        
        return jsonify({
            "status": "Cache available",
            "elements_count": len(cached_data['elements']),
            "last_update": time.ctime(last_cache_update),
            "cache_refresh_count": cache_refresh_count
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
    # Refresh the POTA reference data periodically. OSM data is fetched per
    # request for the requested bounding box to avoid a costly global query.
    schedule.every(1).hours.do(update_pota_data, force=True)

    # Create and start the scheduler thread if it's not already running
    if schedule_thread is None or not schedule_thread.is_alive():
        schedule_thread = Thread(target=run_schedule)
        schedule_thread.daemon = True
        schedule_thread.start()

# Start the scheduler
start_scheduler()

if __name__ == '__main__':
    # Run the Flask app
    app.run(debug=True, host='0.0.0.0', port=5005)
