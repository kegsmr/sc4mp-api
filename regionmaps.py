"""Region maps: each SC4 savegame contains the image SC4 shows for that city in the region view, which are put together like the region view.

Run as a script to make a region map from a folder of savegames:

	python regionmaps.py <region folder> [output.png] [--width 1600]

The region folder is eg. a server's `Regions/<region>` folder or one in `Documents/SimCity 4/Regions`. Needs Pillow.
"""

import hashlib
import heapq
import io
import json
import os
import random
import re
import shutil
import struct
import sys
import tempfile
from argparse import ArgumentParser
from pathlib import Path

try:
	from PIL import Image
	sc4mp_has_pillow = True
except ImportError:
	sc4mp_has_pillow = False

from core.dbpf import SC4Savegame


SC4MP_CITIES_PATH = Path("_SC4MP") / "_Cache" / "Cities"	# each savegame's region view image and location, by MD5
SC4MP_MAP_WIDTH = 1600					# default width of a region map

X = (90.5, 17.75)	# screen offset of one tile along a city's x axis in the region view
Y = (-37.25, 45.0)	# screen offset of one tile along a city's y axis


def is_md5(checksum):

	return isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{32}", checksum) is not None


def write_atomic(path, data):

	temp = path.with_name(path.name + f".{os.getpid()}.{random.randint(0, 1 << 30)}.tmp")
	temp.write_bytes(data)
	os.replace(temp, path)


def city_cached(checksum):

	return (SC4MP_CITIES_PATH / f"{checksum}.json").exists()


def load_city(checksum):

	try:
		with open(SC4MP_CITIES_PATH / f"{checksum}.json", encoding="utf-8") as file:
			return json.load(file)
	except Exception:
		return None


def is_default_ground(r, g, b):
	"""Whether a pixel is the plain blue-grey (or white) ground SC4 draws when a city's terrain textures are missing, eg. when it was saved without the region's terrain mod."""

	return (168 <= r <= 215 and g >= r - 2 and 2 <= b - r <= 24 and max(r, g, b) - min(r, g, b) <= 24) or (min(r, g, b) >= 240 and max(r, g, b) - min(r, g, b) <= 6)


def clean_city_image(data):
	"""Makes the opaque black background some region view images have (SC4's empty city image) transparent. Returns the PNG, the share of the city drawn with the default ground, and its most common other color."""

	image = Image.open(io.BytesIO(data)).convert("RGBA")
	width, height = image.size
	pixels = image.load()

	# Black background, from the edges inwards
	if pixels[0, 0] == (0, 0, 0, 255) or pixels[width - 1, 0] == (0, 0, 0, 255):
		stack = [(x, y) for x in range(width) for y in (0, height - 1)] + [(x, y) for x in (0, width - 1) for y in range(height)]
		while stack:
			x, y = stack.pop()
			r, g, b, a = pixels[x, y]
			if a and r + g + b <= 24:
				pixels[x, y] = (0, 0, 0, 0)
				stack += [(x2, y2) for x2, y2 in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)) if 0 <= x2 < width and 0 <= y2 < height]
		output = io.BytesIO()
		image.save(output, format="PNG", optimize=True)
		data = output.getvalue()

	opaque = default = 0
	colors = {}
	for r, g, b, a in image.getdata():
		if a < 128:
			continue
		opaque += 1
		if is_default_ground(r, g, b):
			default += 1
		else:
			color = (r // 8 * 8 + 4, g // 8 * 8 + 4, b // 8 * 8 + 4)
			colors[color] = colors.get(color, 0) + 1

	return data, round(default / max(1, opaque), 3), list(max(colors, key=colors.get)) if colors else None


def repaint_default_ground(image, color, seed):
	"""Repaints the default ground in `color` (the region's usual ground), keeping its shading and whatever is built on it."""

	rng = random.Random(seed)
	pixels = image.load()
	for y in range(image.height):
		for x in range(image.width):
			r, g, b, a = pixels[x, y]
			if a and is_default_ground(r, g, b):
				shade = (r + g + b) / (744 if min(r, g, b) >= 240 else 588) * (1 + rng.uniform(-0.06, 0.06))	# relative to flat grey or white ground
				pixels[x, y] = (min(255, int(color[0] * shade)), min(255, int(color[1] * shade)), min(255, int(color[2] * shade)), a)
	return image


def cache_city(path, checksum):
	"""Saves a savegame's region view image (the PNG subfile shown for the city in SC4's region view) and its location in the city cache, by its MD5, for the region maps."""

	try:

		savegame = SC4Savegame(path, error_callback=lambda message: None)	# the reader reports some harmless decompression warnings (eg. trailing bytes) through this; without a callback they raise
		try:
			info = savegame.get_SC4ReadRegionalCity()
			image = None
			for entry in savegame.indexData:
				if entry['typeID'] == '8a2482b9' and entry['instanceID'] == '00000000':
					savegame.file.seek(entry['offset'])
					image = savegame.file.read(entry['filesize'])
					break
		finally:
			savegame.close()

		if not image or image[:8] != b"\x89PNG\r\n\x1a\n":
			raise ValueError("The savegame has no region view image.")

		width, height = struct.unpack(">II", image[16:24])
		image, default_ground, ground = clean_city_image(image) if sc4mp_has_pillow else (image, 0, None)

		city = {
			"x": info["tileXLocation"],
			"y": info["tileYLocation"],
			"size": info["citySizeX"],
			"width": width,
			"height": height,
			"city_name": info.get("cityName"),
			"mayor_name": info.get("mayorName"),
			"default_ground": default_ground,
			"ground": ground,
		}

		write_atomic(SC4MP_CITIES_PATH / f"{checksum}.png", image)

	except Exception as e:

		city = {"error": str(e)}	# not downloaded again

	write_atomic(SC4MP_CITIES_PATH / f"{checksum}.json", json.dumps(city).encode())


def region_map_order(cities):
	"""Orders cities back to front, so each one is drawn after the cities it's in front of (those whose +x or +y edge or corner it touches). A single sort key can't do this when cities have different sizes."""

	grid = {}
	for i, city in enumerate(cities):
		for dx in range(city["size"]):
			for dy in range(city["size"]):
				grid[(city["x"] + dx, city["y"] + dy)] = i

	after = [set() for _ in cities]
	waiting = [0] * len(cities)
	for i, city in enumerate(cities):
		x, y, size = city["x"], city["y"], city["size"]
		for cell in [(x + size, y + d) for d in range(size)] + [(x + d, y + size) for d in range(size)] + [(x + size, y + size)]:
			j = grid.get(cell)
			if j is not None and j != i and j not in after[i]:
				after[i].add(j)
				waiting[j] += 1

	key = lambda i: (cities[i]["x"] + cities[i]["y"], i)
	ready = [key(i) for i in range(len(cities)) if waiting[i] == 0]
	heapq.heapify(ready)
	order = []
	while ready:
		_, i = heapq.heappop(ready)
		order.append(i)
		for j in after[i]:
			waiting[j] -= 1
			if waiting[j] == 0:
				heapq.heappush(ready, key(j))

	# Cities in a cycle (eg. a pinwheel of different sizes) are drawn by their near corner
	if len(order) < len(cities):
		done = set(order)
		order += sorted((i for i in range(len(cities)) if i not in done), key=key)

	return order


def region_map_layout(checksums):
	"""Places the cities' region view images like SC4's region view, at full size. Returns the cities in drawing order (back to front), each with its image's `left` and `top`, and the map's size, or None if no city has an image."""

	cities = []
	for checksum in checksums:
		city = load_city(checksum)
		if city and "error" not in city and (SC4MP_CITIES_PATH / f"{checksum}.png").exists():
			city["checksum"] = checksum
			cities.append(city)
	if not cities:
		return None

	# Position of each city's image. The image's left edge is the tile's left corner, and terrain higher than the tile's top corner makes the image taller
	for city in cities:
		top_x = city["x"] * X[0] + city["y"] * Y[0]
		top_y = city["x"] * X[1] + city["y"] * Y[1]
		city["left"] = top_x + city["size"] * Y[0]
		city["top"] = top_y - (city["height"] - (64 * city["size"] + 21))

	min_x = min(city["left"] for city in cities)
	min_y = min(city["top"] for city in cities)
	for city in cities:
		city["left"] -= min_x
		city["top"] -= min_y

	width = max(city["left"] + city["width"] for city in cities)
	height = max(city["top"] + city["height"] for city in cities)

	return [cities[i] for i in region_map_order(cities)], width, height


def city_outline(city):
	"""The city's outline on the full size map, as a list of points: its tile, raised by its tallest terrain, down to the bottom of its edge."""

	size = city["size"]
	raised = city["height"] - (64 * size + 21)
	left = (city["left"], city["top"] + raised + size * Y[1])	# the tile's corners, from its left corner
	top = (left[0] - size * Y[0], left[1] - size * Y[1])
	right = (top[0] + size * X[0], top[1] + size * X[1])
	bottom = (left[0] + size * X[0], left[1] + size * X[1])
	edge = 18	# the brown edge under the tile
	return [(left[0], left[1] - raised), (top[0], top[1] - raised), (right[0], right[1] - raised), (right[0], right[1] + edge), (bottom[0], bottom[1] + edge), (left[0], left[1] + edge)]


def render_region_map(checksums, width):
	"""Assembles a region map from the cities' region view images, like SC4's region view, scaled to `width` pixels or less. Cities drawn with the default ground get the region's usual ground instead."""

	layout = region_map_layout(checksums)
	if layout is None:
		return None
	cities, full_width, full_height = layout

	grounds = sorted(city["ground"] for city in cities if city.get("ground") and city.get("default_ground", 0) < 0.1)
	ground = [sorted(color[i] for color in grounds)[len(grounds) // 2] for i in range(3)] if grounds else [68, 76, 44]

	scale = min(1, width / full_width)
	canvas = Image.new("RGBA", (int(full_width * scale) + 1, int(full_height * scale) + 1), (0, 0, 0, 0))

	for city in cities:
		with Image.open(SC4MP_CITIES_PATH / f"{city['checksum']}.png") as image:
			image = image.convert("RGBA")
			if city.get("default_ground", 0) >= 0.15:
				image = repaint_default_ground(image, ground, city["checksum"])
			if scale < 1:
				image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
			canvas.alpha_composite(image, (round(city["left"] * scale), round(city["top"] * scale)))

	return canvas


def main():

	parser = ArgumentParser(description="Makes a region map from a folder of SC4 savegames.")
	parser.add_argument("region", help="folder with the region's savegames")
	parser.add_argument("output", nargs="?", help="PNG file to write (default: <region name>.png)")
	parser.add_argument("--width", type=int, default=SC4MP_MAP_WIDTH, help=f"maximum width in pixels (default: {SC4MP_MAP_WIDTH}, 0 for full size)")
	args = parser.parse_args()

	if not sc4mp_has_pillow:
		sys.exit("Pillow is required: pip install Pillow")

	global SC4MP_CITIES_PATH

	region = Path(args.region)
	output = Path(args.output or f"{region.name}.png")

	# Use a temporary city cache, so nothing is left behind
	SC4MP_CITIES_PATH = Path(tempfile.mkdtemp(prefix="sc4mp_regionmap_"))

	try:

		checksums = []
		for path in sorted(region.glob("*.sc4")):
			checksum = hashlib.md5(path.read_bytes()).hexdigest()
			cache_city(path, checksum)
			city = load_city(checksum)
			if "error" in city:
				print(f'Skipped "{path.name}": {city["error"]}')
			else:
				checksums.append(checksum)

		print(f"{len(checksums)} cities in {region.name}")

		image = render_region_map(checksums, args.width or 10 ** 9)
		if image is None:
			sys.exit("No cities with region view images found.")

		image.save(output)
		print(f'Saved "{output}" ({image.width}x{image.height})')

	finally:

		shutil.rmtree(SC4MP_CITIES_PATH, ignore_errors=True)


if __name__ == "__main__":
	main()
