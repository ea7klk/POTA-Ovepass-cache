import express from 'express';
import axios from 'axios';
import csv from 'csv-parser';
import schedule from 'node-schedule';
import { parse } from 'url';
import osmtogeojson from 'osmtogeojson';
import { Readable } from 'stream';
import geobuf from 'geobuf';
import Pbf from 'pbf';

const app = express();
const port = 5001;

let potaOriCache = {
  type: "FeatureCollection",
  features: []
};

let potaOsmCache = {
  type: "FeatureCollection",
  features: []
};

// Function to fetch and parse CSV data
async function fetchCSVData() {
  const url = 'https://pota.app/all_parks_ext.csv';
  const response = await axios.get(url);
  const data = response.data;
  const results = [];

  return new Promise((resolve, reject) => {
    Readable.from(data)
      .pipe(csv())
      .on('data', (row) => {
        if (row.active === '1') {
          results.push(row);
        }
      })
      .on('end', () => {
        resolve(results);
      })
      .on('error', reject);
  });
}

// Function to convert CSV data to GeoJSON
function convertCSVToGeoJSON(csvData) {
  return {
    type: "FeatureCollection",
    features: csvData.map(row => ({
      type: "Feature",
      properties: {
        reference: row.reference,
        name: row.name,
        active: row.active,
        entityId: row.entityId,
        locationDesc: row.locationDesc,
        grid: row.grid
      },
      geometry: {
        type: "Point",
        coordinates: [parseFloat(row.longitude), parseFloat(row.latitude)]
      }
    }))
  };
}

// Function to fetch Overpass API data
async function fetchOverpassData() {
  const query = `
    [out:json];
    nwr["communication:amateur_radio:pota"];
    out geom;
  `;
  const url = `https://overpass-api.de/api/interpreter?data=${encodeURIComponent(query)}`;
  const response = await axios.get(url);
  return response.data;
}

// Function to convert Overpass data to GeoJSON using osmtogeojson
function convertOverpassToGeoJSON(overpassData) {
  if (!overpassData || !overpassData.elements) {
    throw new Error('Invalid Overpass data');
  }

  const geojson = osmtogeojson(overpassData);
  geojson.features.forEach(feature => {
    if (feature.properties["communication:amateur_radio:pota"]) {
      feature.properties.feature = feature.properties["communication:amateur_radio:pota"];
      feature.properties.reference = feature.properties["communication:amateur_radio:pota"];
    }
    feature.properties.isInOSM = "true";
  });
  return geojson;
}

// Schedule tasks
schedule.scheduleJob('0 * * * *', async () => {
  console.log('Fetching CSV data...');
  const csvData = await fetchCSVData();
  potaOriCache = convertCSVToGeoJSON(csvData);
  console.log('CSV data updated.');
});

schedule.scheduleJob('*/5 * * * *', async () => {
  console.log('Fetching Overpass data...');
  try {
    const overpassData = await fetchOverpassData();
    potaOsmCache = convertOverpassToGeoJSON(overpassData);
    console.log('Overpass data updated.');
  } catch (error) {
    console.error('Error fetching or converting Overpass data:', error);
  }
});

// Run tasks on server startup
(async () => {
  console.log('Fetching initial CSV data...');
  const csvData = await fetchCSVData();
  potaOriCache = convertCSVToGeoJSON(csvData);
  console.log('Initial CSV data fetched and cached.');

  console.log('Fetching initial Overpass data...');
  try {
    const overpassData = await fetchOverpassData();
    potaOsmCache = convertOverpassToGeoJSON(overpassData);
    console.log('Initial Overpass data fetched and cached.');
  } catch (error) {
    console.error('Error fetching or converting initial Overpass data:', error);
  }
})();

// Function to filter features by bounding box
function filterFeaturesByBBox(features, bbox) {
  const [minLon, minLat, maxLon, maxLat] = bbox.split(',').map(Number);
  return features.filter(feature => {
    const { type, coordinates } = feature.geometry;
    if (type === 'Point') {
      const [lon, lat] = coordinates;
      return lon >= minLon && lon <= maxLon && lat >= minLat && lat <= maxLat;
    } else if (type === 'Polygon' || type === 'MultiPolygon') {
      return coordinates.some(ring => ring.some(([lon, lat]) => lon >= minLon && lon <= maxLon && lat >= minLat && lat <= maxLat));
    } else if (type === 'LineString') {
      return coordinates.some(([lon, lat]) => lon >= minLon && lon <= maxLon && lat >= minLat && lat <= maxLat);
    }
    return false;
  });
}

// Endpoint to get pota-ori GeoJSON data with bounding box
app.get('/pota-ori/geojson', (req, res) => {
  const { bbox } = parse(req.url, true).query;
  if (!bbox) {
    return res.status(400).send('Bounding box (bbox) query parameter is required.');
  }

  const filteredFeatures = filterFeaturesByBBox(potaOriCache.features, bbox);
  res.json({
    type: "FeatureCollection",
    features: filteredFeatures
  });
});

// Endpoint to get pota-ori Geobuf data with bounding box
app.get('/pota-ori/geobuf', (req, res) => {
  const { bbox } = parse(req.url, true).query;
  if (!bbox) {
    return res.status(400).send('Bounding box (bbox) query parameter is required.');
  }

  const filteredFeatures = filterFeaturesByBBox(potaOriCache.features, bbox);
  const filteredGeoJSON = {
    type: "FeatureCollection",
    features: filteredFeatures
  };

  const geobufData = geobuf.encode(filteredGeoJSON, new Pbf());
  res.set('Content-Type', 'application/octet-stream');
  res.send(Buffer.from(geobufData));
});

// Endpoint to get pota-osm GeoJSON data with bounding box
app.get('/pota-osm/geojson', (req, res) => {
  const { bbox } = parse(req.url, true).query;
  if (!bbox) {
    return res.status(400).send('Bounding box (bbox) query parameter is required.');
  }

  const filteredFeatures = filterFeaturesByBBox(potaOsmCache.features, bbox);
  res.json({
    type: "FeatureCollection",
    features: filteredFeatures
  });
});

// Endpoint to get pota-osm Geobuf data with bounding box
app.get('/pota-osm/geobuf', (req, res) => {
  const { bbox } = parse(req.url, true).query;
  if (!bbox) {
    return res.status(400).send('Bounding box (bbox) query parameter is required.');
  }

  const filteredFeatures = filterFeaturesByBBox(potaOsmCache.features, bbox);
  const filteredGeoJSON = {
    type: "FeatureCollection",
    features: filteredFeatures
  };

  const geobufData = geobuf.encode(filteredGeoJSON, new Pbf());
  res.set('Content-Type', 'application/octet-stream');
  res.send(Buffer.from(geobufData));
});

app.listen(port, () => {
  console.log(`Server is running on http://localhost:${port}`);
});
