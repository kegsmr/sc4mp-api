"""Region maps: each SC4 savegame contains the image SC4 shows for that city in the region view, which are put together like the region view.

Run as a script to make a region map from a folder of savegames:

	python regionmaps.py <region folder> [output.png] [--width 1600]

The region folder is eg. a server's `Regions/<region>` folder or one in `Documents/SimCity 4/Regions`. Needs Pillow.
"""

import hashlib
import heapq
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

		city = {
			"x": info["tileXLocation"],
			"y": info["tileYLocation"],
			"size": info["citySizeX"],
			"width": width,
			"height": height,
			"city_name": info.get("cityName"),
			"mayor_name": info.get("mayorName"),
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


def render_region_map(checksums, width):
	"""Assembles a region map from the cities' region view images, like SC4's region view, scaled to `width` pixels or less."""

	X = (90.5, 17.75)	# screen offset of one tile along a city's x axis
	Y = (-37.25, 45.0)	# screen offset of one tile along a city's y axis

	cities = []
	for checksum in checksums:
		city = load_city(checksum)
		if city and "error" not in city and (SC4MP_CITIES_PATH / f"{checksum}.png").exists():
			city["checksum"] = checksum
			cities.append(city)
	if not cities:
		return None

	# Position of each city's image at full size. The image's left edge is the tile's left corner, and terrain higher than the tile's top corner makes the image taller
	for city in cities:
		top_x = city["x"] * X[0] + city["y"] * Y[0]
		top_y = city["x"] * X[1] + city["y"] * Y[1]
		city["left"] = top_x + city["size"] * Y[0]
		city["top"] = top_y - (city["height"] - (64 * city["size"] + 21))

	min_x = min(city["left"] for city in cities)
	min_y = min(city["top"] for city in cities)
	max_x = max(city["left"] + city["width"] for city in cities)
	max_y = max(city["top"] + city["height"] for city in cities)

	scale = min(1, width / (max_x - min_x))
	canvas = Image.new("RGBA", (int((max_x - min_x) * scale) + 1, int((max_y - min_y) * scale) + 1), (0, 0, 0, 0))

	for i in region_map_order(cities):
		city = cities[i]
		with Image.open(SC4MP_CITIES_PATH / f"{city['checksum']}.png") as image:
			image = image.convert("RGBA")
			if scale < 1:
				image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
			canvas.alpha_composite(image, (round((city["left"] - min_x) * scale), round((city["top"] - min_y) * scale)))

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
