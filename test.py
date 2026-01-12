import h3
# Coordinates from your logs
lat, lng = 29.7686728, -95.3381552
print(h3.latlng_to_cell(lat, lng, 8))