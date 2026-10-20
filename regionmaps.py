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
	from PIL import Image, ImageDraw
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


def is_snow(r, g, b):
	"""Whether a pixel is snow-covered ground (blue-grey or white), eg. from a winter terrain mod."""

	return (168 <= r <= 215 and g >= r - 2 and 2 <= b - r <= 24 and max(r, g, b) - min(r, g, b) <= 24) or (min(r, g, b) >= 240 and max(r, g, b) - min(r, g, b) <= 6)


def clean_city_image(data):
	"""Makes the opaque black background some region view images have (SC4's empty city image) transparent. Returns the PNG, the share of the city covered in snow, its most common land and water colors, and whether it was the empty city image."""

	image = Image.open(io.BytesIO(data)).convert("RGBA")
	width, height = image.size
	pixels = image.load()

	# Black background, from the edges inwards
	blank = pixels[0, 0] == (0, 0, 0, 255) or pixels[width - 1, 0] == (0, 0, 0, 255)
	if blank:
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

	opaque = snow = 0
	land = {}
	water = {}
	for r, g, b, a in image.getdata():
		if a < 128:
			continue
		opaque += 1
		if is_snow(r, g, b):
			snow += 1
			continue
		color = (r // 8 * 8 + 4, g // 8 * 8 + 4, b // 8 * 8 + 4)
		if b > r + 16 and b > g + 8:
			water[color] = water.get(color, 0) + 1
		elif max(r, g, b) - min(r, g, b) >= 16:	# not the grey edges, roads or buildings
			land[color] = land.get(color, 0) + 1

	most = lambda colors: list(max(colors, key=colors.get)) if colors else None
	return data, round(snow / max(1, opaque), 3), most(land), most(water), blank


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
		image, snow, ground, water, blank = clean_city_image(image) if sc4mp_has_pillow else (image, 0, None, None, False)

		city = {
			"x": info["tileXLocation"],
			"y": info["tileYLocation"],
			"size": info["citySizeX"],
			"width": width,
			"height": height,
			"city_name": info.get("cityName"),
			"mayor_name": info.get("mayorName"),
			"snow": snow,
			"ground": ground,
			"water": water,
			"blank": blank,
		}

		write_atomic(SC4MP_CITIES_PATH / f"{checksum}.png", image)

	except Exception as e:

		city = {"error": str(e)}	# not downloaded again

	write_atomic(SC4MP_CITIES_PATH / f"{checksum}.json", json.dumps(city).encode())


def water_under(image, city, water):
	"""Puts water under a city's region view image, at its tile's base. SC4 draws the sea separately in the region view, so where a city is low its image is see-through."""

	size = city["size"]
	raised = image.height - (64 * size + 21)
	left = (0, raised + size * Y[1])
	top = (-size * Y[0], raised)
	right = (top[0] + size * X[0], raised + size * X[1])
	bottom = (size * X[0], left[1] + size * X[1])
	edge = 18
	under = Image.new("RGBA", image.size, (0, 0, 0, 0))
	ImageDraw.Draw(under).polygon([left, top, right, (right[0], right[1] + edge), (bottom[0], bottom[1] + edge), (left[0], left[1] + edge)], fill=tuple(water) + (255,))
	under.alpha_composite(image)
	return under


def read_region_config(path):
	"""Reads a region's `config.bmp`: each tile's position and size (reddish pixels are small tiles, greenish medium and bluish large, like SC4)."""

	with Image.open(path) as image:
		image = image.convert("RGB")
		width, height = image.size
		pixels = image.load()

	tiles = []
	covered = set()
	for y in range(height):
		for x in range(width):
			if (x, y) in covered:
				continue
			r, g, b = pixels[x, y]
			if max(r, g, b) < 128:
				continue
			size = 1 if r >= max(g, b) else 2 if g >= b else 4
			tiles.append((x, y, size))
			covered.update((x + dx, y + dy) for dx in range(size) for dy in range(size))

	return tiles


SC4MP_BLANK_CITY = None	# path of a cached copy of SC4's empty (large) city image, for the region's empty tiles


def empty_tile_image(size, ground):
	"""The image of an empty tile: SC4's empty city image, cut down to the tile's size from its front corner (so it keeps the edges) and tinted to the region's usual ground. Drawn plainly if no empty city image has been cached yet."""

	global SC4MP_BLANK_CITY

	if SC4MP_BLANK_CITY is None:
		SC4MP_BLANK_CITY = ""
		for path in SC4MP_CITIES_PATH.glob("*.json"):
			city = load_city(path.stem)
			if city and city.get("blank") and city.get("size") == 4 and path.with_suffix(".png").exists():
				SC4MP_BLANK_CITY = str(path.with_suffix(".png"))
				break

	width, height = 128 * size + 2, 64 * size + 21
	edge = 21
	left = (0, size * Y[1])
	top = (-size * Y[0], 0)
	right = (top[0] + size * X[0], size * X[1])
	bottom = (size * X[0], left[1] + size * X[1])
	outline = [left, top, right, (right[0], right[1] + edge), (bottom[0], bottom[1] + edge), (left[0], left[1] + edge)]

	if SC4MP_BLANK_CITY:

		with Image.open(SC4MP_BLANK_CITY) as source:
			source = source.convert("RGBA")

		# The front s x s part of the large tile
		raised = source.height - (64 * 4 + 21)
		offset = 4 - size
		corner = (4 * -Y[0] + offset * (X[0] + Y[0]) + size * Y[0], raised + offset * (X[1] + Y[1]))
		image = source.crop((round(corner[0]), round(corner[1]), round(corner[0]) + width, round(corner[1]) + height))
		mask = Image.new("L", image.size, 0)
		ImageDraw.Draw(mask).polygon(outline, fill=255)
		alpha = image.getchannel("A")
		image.putalpha(Image.composite(alpha, mask, mask))

		# Tinted to the region's ground (the empty city image is grass)
		r, g, b = (sum(c) / max(1, len(c)) for c in zip(*[p[:3] for p in image.crop((width // 3, height // 3, 2 * width // 3, height // 2)).getdata()]))
		bands = image.split()
		tint = [ground[0] / max(1, r), ground[1] / max(1, g), ground[2] / max(1, b)]
		tops = Image.new("L", image.size, 0)
		ImageDraw.Draw(tops).polygon([left, top, right, (right[0], right[1] + 1), (bottom[0], bottom[1] + 1), (left[0], left[1] + 1)], fill=255, outline=255)	# only the ground, not the edges
		tinted = [band.point(lambda v, t=t: min(255, int(v * t))) for band, t in zip(bands[:3], tint)]
		image = Image.merge("RGBA", [Image.composite(t, band, tops) for t, band in zip(tinted, bands[:3])] + [bands[3]])
		return image

	image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
	draw = ImageDraw.Draw(image)
	draw.polygon([left, bottom, (bottom[0], bottom[1] + edge), (left[0], left[1] + edge)], fill=(112, 106, 98, 255))
	draw.polygon([bottom, right, (right[0], right[1] + edge), (bottom[0], bottom[1] + edge)], fill=(84, 80, 74, 255))
	draw.polygon([left, top, right, bottom], fill=tuple(ground) + (255,))
	return image


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


def region_map_layout(checksums, tiles=None):
	"""Places the cities' region view images like SC4's region view, at full size. The region's other tiles (from `read_region_config`) are added as empty tiles (with `"empty": True`). Returns the cities in drawing order (back to front), each with its image's `left` and `top`, and the map's size, or None if no city has an image."""

	cities = []
	for checksum in checksums:
		city = load_city(checksum)
		if city and "error" not in city and (SC4MP_CITIES_PATH / f"{checksum}.png").exists():
			city["checksum"] = checksum
			cities.append(city)
	if not cities:
		return None

	# Tiles without a city
	covered = set((city["x"] + dx, city["y"] + dy) for city in cities for dx in range(city["size"]) for dy in range(city["size"]))
	for x, y, size in tiles or []:
		cells = set((x + dx, y + dy) for dx in range(size) for dy in range(size))
		if not cells & covered:
			cities.append({"x": x, "y": y, "size": size, "width": 128 * size + 2, "height": 64 * size + 21, "empty": True})
			covered |= cells

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


def render_region_map(checksums, width, tiles=None):
	"""Assembles a region map from the cities' region view images, like SC4's region view, scaled to `width` pixels or less. The region's tiles without a city (from `read_region_config`) are drawn empty."""

	layout = region_map_layout(checksums, tiles)
	if layout is None:
		return None
	cities, full_width, full_height = layout

	def median(colors, default):
		colors = list(colors)
		return [sorted(color[i] for color in colors)[len(colors) // 2] for i in range(3)] if colors else default

	ground = median((city["ground"] for city in cities if city.get("ground") and city.get("snow", 0) < 0.1 and not city.get("blank")), [68, 76, 44])	# for the empty tiles

	scale = min(1, width / full_width)
	canvas = Image.new("RGBA", (int(full_width * scale) + 1, int(full_height * scale) + 1), (0, 0, 0, 0))

	empty_tiles = {}
	for city in cities:
		if city.get("empty"):
			if city["size"] not in empty_tiles:
				empty_tiles[city["size"]] = empty_tile_image(city["size"], ground)
			image = empty_tiles[city["size"]]
			if scale < 1:
				image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
			canvas.alpha_composite(image, (round(city["left"] * scale), round(city["top"] * scale)))
			continue
		with Image.open(SC4MP_CITIES_PATH / f"{city['checksum']}.png") as image:
			image = image.convert("RGBA")
			if city.get("water"):	# only under cities with water, so eg. Mars doesn't get water in the gaps between its tiles
				image = water_under(image, city, city["water"])
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

		tiles = read_region_config(region / "config.bmp") if (region / "config.bmp").exists() else None
		image = render_region_map(checksums, args.width or 10 ** 9, tiles)
		if image is None:
			sys.exit("No cities with region view images found.")

		image.save(output)
		print(f'Saved "{output}" ({image.width}x{image.height})')

	finally:

		shutil.rmtree(SC4MP_CITIES_PATH, ignore_errors=True)


if __name__ == "__main__":
	main()
