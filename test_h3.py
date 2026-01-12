import h3

# A test coordinate in Houston (near Hermann Park)
lat, lng = 29.7136, -95.3895

# 🟢 NEW SYNTAX for Version 4.x
hex_id = h3.latlng_to_cell(lat, lng, 8)

print(f"Coordinate: {lat}, {lng}")
print(f"H3 Hexagon ID: {hex_id}")

# Simulate a driver's Red Zone list (use the IDs from your SQL output)
driver_red_zones = ["8826a100d3fffff", "8826a100d1fffff"]

# Check if we are inside
is_red = hex_id in driver_red_zones
print(f"Is this a Red Zone? {is_red}")