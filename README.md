# Overpass POTA Server

This is a simple server that caches POTA (Parks on the Air) data from the Overpass API and provides filtered results based on a given bounding box.

## Setup

1. Ensure you have Python 3.7+ installed.
2. Install the required packages:

```
pip install -r requirements.txt
```

## Running the server

To start the server, run:

```
python server.py
```

The server will start on `http://localhost:5000`.

## Usage

To query the server, send a GET request to the `/query` endpoint with the following parameters:

- `south`: Southern latitude of the bounding box
- `west`: Western longitude of the bounding box
- `north`: Northern latitude of the bounding box
- `east`: Eastern longitude of the bounding box

Example:

```
http://localhost:5000/query?south=40&west=-100&north=50&east=-90
```

The server will return a JSON response with the filtered POTA locations within the specified bounding box.

## Features

- Caches POTA data from Overpass API
- Updates cache every 6 hours
- Filters cached data based on provided bounding box
- Handles both node and way/relation geometries