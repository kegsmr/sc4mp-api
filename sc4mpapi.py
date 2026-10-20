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
import time
import traceback
import urllib.parse
from argparse import ArgumentParser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from inspect import stack
from os import unlink
from pathlib import Path
from socket import socket
from threading import Thread, current_thread

try:
	from flask import Flask, jsonify, abort, send_from_directory, request, send_file
	app = Flask(__name__)
	sc4mp_has_flask = True
except ImportError:
	sc4mp_has_flask = False
	# Stand-in so the route decorators below don't fail at import when Flask is missing
	class NoFlask:
		def after_request(self, function):
			return function
		def route(self, *args, **kwargs):
			return lambda function: function
	app = NoFlask()

from core.networking import \
	ClientSocket, NetworkException, ConnectionClosedException, \
	send_json, recv_json, BUFFER_SIZE

from core.dbpf import SC4Savegame

try:
	from PIL import Image, ImageDraw
	sc4mp_has_pillow = True
except ImportError:
	sc4mp_has_pillow = False


SC4MP_TITLE = "SC4MP API"

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]

SC4MP_BUFFER_SIZE = BUFFER_SIZE

SC4MP_SERVER_EXPIRY = 30 # days

# Region maps
SC4MP_CITIES_PATH = Path("_SC4MP") / "_Cache" / "Cities"	# each savegame's region view image and location, by MD5
SC4MP_MAPS_PATH = Path("_SC4MP") / "_Cache" / "Maps"		# assembled region maps
SC4MP_MAP_WIDTH = 1600					# default width of a region map
SC4MP_MAP_DOWNLOAD_LIMIT = 100_000_000	# bytes of new savegames downloaded per server per scan, so big servers fill in over a few scans
SC4MP_MAP_WIDTHS = (400, 800, 1600, 4096)	# widths a region map is rendered at (requested widths are rounded up to one of these)
SC4MP_MAP_X = (90.5, 17.75)	# screen offset of one tile along a city's x axis in the region view
SC4MP_MAP_Y = (-37.25, 45.0)	# screen offset of one tile along a city's y axis


def init():

	global sc4mp_scanner

	sys.stdout = Logger()
	current_thread().name = "Main"

	print(SC4MP_TITLE)

	SC4MP_CITIES_PATH.mkdir(parents=True, exist_ok=True)
	SC4MP_MAPS_PATH.mkdir(parents=True, exist_ok=True)

	print("Starting scanner...")

	sc4mp_scanner = Scanner()
	sc4mp_scanner.start()


def main():

	global sc4mp_args

	sc4mp_args = parse_args()

	print("Starting webserver...")

	if sc4mp_has_flask:

		print(f"Webserver started on http://localhost:{sc4mp_args.port}")

		try:
			app.run(host=sc4mp_args.host, port=int(sc4mp_args.port), debug=False)
		except KeyboardInterrupt:
			pass

		print("Webserver stopped.")

	else:

		print("[WARNING] Flask is unavailable. Webserver cannot start.")

		try:
			while True:
				time.sleep(.1)
		except KeyboardInterrupt:
			pass

	print("Stopping scanner...")

	sc4mp_scanner.end = True


def parse_args():

	parser = ArgumentParser()

	parser.add_argument("--host", required=False)
	parser.add_argument("--port", required=False)

	return parser.parse_args()


def get_bitmap_dimensions(filename):

	with open(filename, "rb") as file:
		data = bytearray(file.read())

	width = struct.unpack_from('<i', data, 18)
	height = struct.unpack_from('<i', data, 22)

	return (width[0], height[0])


# ===== Region maps =====

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
	left = (0, raised + size * SC4MP_MAP_Y[1])
	top = (-size * SC4MP_MAP_Y[0], raised)
	right = (top[0] + size * SC4MP_MAP_X[0], raised + size * SC4MP_MAP_X[1])
	bottom = (size * SC4MP_MAP_X[0], left[1] + size * SC4MP_MAP_X[1])
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
	left = (0, size * SC4MP_MAP_Y[1])
	top = (-size * SC4MP_MAP_Y[0], 0)
	right = (top[0] + size * SC4MP_MAP_X[0], size * SC4MP_MAP_X[1])
	bottom = (size * SC4MP_MAP_X[0], left[1] + size * SC4MP_MAP_X[1])
	outline = [left, top, right, (right[0], right[1] + edge), (bottom[0], bottom[1] + edge), (left[0], left[1] + edge)]

	if SC4MP_BLANK_CITY:

		with Image.open(SC4MP_BLANK_CITY) as source:
			source = source.convert("RGBA")

		# The front s x s part of the large tile
		raised = source.height - (64 * 4 + 21)
		offset = 4 - size
		corner = (4 * -SC4MP_MAP_Y[0] + offset * (SC4MP_MAP_X[0] + SC4MP_MAP_Y[0]) + size * SC4MP_MAP_Y[0], raised + offset * (SC4MP_MAP_X[1] + SC4MP_MAP_Y[1]))
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
		top_x = city["x"] * SC4MP_MAP_X[0] + city["y"] * SC4MP_MAP_Y[0]
		top_y = city["x"] * SC4MP_MAP_X[1] + city["y"] * SC4MP_MAP_Y[1]
		city["left"] = top_x + city["size"] * SC4MP_MAP_Y[0]
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
	left = (city["left"], city["top"] + raised + size * SC4MP_MAP_Y[1])	# the tile's corners, from its left corner
	top = (left[0] - size * SC4MP_MAP_Y[0], left[1] - size * SC4MP_MAP_Y[1])
	right = (top[0] + size * SC4MP_MAP_X[0], top[1] + size * SC4MP_MAP_X[1])
	bottom = (left[0] + size * SC4MP_MAP_X[0], left[1] + size * SC4MP_MAP_X[1])
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


def show_error(e):

	message = None
	if isinstance(e, str):
		message = e
	else:
		message = str(e)

	print("[ERROR] " + message + "\n\n" + traceback.format_exc())


@app.after_request
def add_cors_headers(response):

	response.headers['Access-Control-Allow-Origin'] = '*'

	return response


@app.route('/.well-known/acme-challenge/<filename>')
def serve_challenge(filename):

    challenge_directory = os.path.join(os.getcwd(), '.well-known', 'acme-challenge')

    return send_from_directory(challenge_directory, filename)


@app.route("/servers", methods=["GET"])
def get_servers():

	return jsonify(list(sc4mp_scanner.servers.values()))


@app.route("/servers/<server_id>", methods=["GET"])
def get_server(server_id):

    server = sc4mp_scanner.servers.get(server_id)

    if server is None:
        abort(404)

    return jsonify(server)


@app.route("/servers/<server_id>/regions", methods=["GET"])
def get_server_regions(server_id):

	regions = sc4mp_scanner.region_maps.get(server_id)

	if regions is None:
		abort(404)

	return jsonify({
		region: {
			"cities": len(checksums),
			"ready": sum(1 for checksum in checksums if city_cached(checksum)),
			"map": f"/servers/{urllib.parse.quote(server_id, safe='')}/regions/{urllib.parse.quote(region, safe='')}/map.png",
			"map_cities": f"/servers/{urllib.parse.quote(server_id, safe='')}/regions/{urllib.parse.quote(region, safe='')}/map.json",
		}
		for region, checksums in regions.items()
	})


@app.route("/servers/<server_id>/regions/<region>/map.png", methods=["GET"])
def get_region_map(server_id, region):

	checksums = sc4mp_scanner.region_maps.get(server_id, {}).get(region)

	if checksums is None:
		abort(404)

	if not sc4mp_has_pillow:
		abort(503)

	try:
		requested_width = int(request.args.get("width", SC4MP_MAP_WIDTH))
	except ValueError:
		abort(400)

	# Only render a few sizes, so requests for every width can't each make a new render and cache file
	width = SC4MP_MAP_WIDTHS[-1]
	for size in SC4MP_MAP_WIDTHS:
		if requested_width <= size:
			width = size
			break

	tiles = sc4mp_scanner.region_tiles.get(server_id, {}).get(region)

	# Maps are cached until a city in the region changes
	ready = sorted(checksum for checksum in checksums if city_cached(checksum))
	key = hashlib.sha1(json.dumps([server_id, region, width]).encode()).hexdigest()[:16]
	version = hashlib.sha1(json.dumps([ready, tiles]).encode()).hexdigest()[:16]
	path = SC4MP_MAPS_PATH / f"{key}-{version}.png"

	if not path.exists():
		image = render_region_map(ready, width, tiles)
		if image is None:
			abort(404)
		temp = path.with_name(path.name + f".{os.getpid()}.{random.randint(0, 1 << 30)}.tmp")
		image.save(temp, format="PNG", optimize=True)
		os.replace(temp, path)
		for old in SC4MP_MAPS_PATH.glob(f"{key}-*.png"):	# older versions of this map
			if old != path:
				old.unlink(missing_ok=True)

	return send_file(str(path.resolve()), mimetype="image/png", max_age=300)


@app.route("/servers/<server_id>/regions/<region>/map.json", methods=["GET"])
def get_region_map_cities(server_id, region):
	"""Where each city is on the region map (its outline, as fractions of the map's width and height, front to back) and its stats, for showing city info over the map."""

	checksums = sc4mp_scanner.region_maps.get(server_id, {}).get(region)

	if checksums is None:
		abort(404)

	tiles = sc4mp_scanner.region_tiles.get(server_id, {}).get(region)
	layout = region_map_layout([checksum for checksum in checksums if city_cached(checksum)], tiles)
	if layout is None:
		abort(404)
	cities, width, height = layout

	stats = sc4mp_scanner.region_cities.get(server_id, {}).get(region, {})

	return jsonify({
		"width": width,
		"height": height,
		"cities": [
			dict(
				{"x": city["x"], "y": city["y"], "size": city["size"], "city_name": city.get("city_name"), "mayor_name": city.get("mayor_name")},
				**stats.get(city["checksum"], {}),
				outline=[[round(x / width, 5), round(y / height, 5)] for x, y in city_outline(city)],
			)
			for city in reversed(cities) if not city.get("empty")
		],
	})


class Scanner(Thread):


	def __init__(self):

		super().__init__()
		self.daemon = True

		self.MAX_THREADS = 50

		self.new_servers = dict()
		self.servers = self.new_servers
		self.server_queue = SC4MP_SERVERS.copy()
		self.thread_count = 0
		self.region_maps = {}	# savegame MD5s in each region, by server ID and region name
		self.region_cities = {}	# each city's stats from the region's region.json, by server ID, region name and savegame MD5
		self.region_tiles = {}	# each region's tiles from its config.bmp, by server ID and region name
		self.end = False


	def run(self):

		try:

			tried_servers = []

			while not self.end:

				try:

					if len(self.server_queue) > 0:

						if self.thread_count < self.MAX_THREADS:

							server = self.server_queue.pop(0)

							if not server in tried_servers:

								#print(f"Fetching server at {server[0]}:{server[1]}...")

								fetcher = self.Fetcher(self, server)
								fetcher.start()

								self.thread_count += 1

								tried_servers.append(server)

						time.sleep(.1)

					else:

						count = 0

						while not self.end:

							if len(self.server_queue) > 0:

								break

							elif count < 60 and self.thread_count > 0:

								count += 1

								time.sleep(10)

							else:

								#try:
								#	shutil.rmtree(os.path.join("_SC4MP", "_Temp", "ServerList"))
								#except Exception as e:
								#	pass

								for server_id, entry in self.servers.items():
									# Drop servers that haven't responded in a while
									updated = entry.get("updated")
									if updated and datetime.utcnow() - datetime.strptime(updated, "%Y-%m-%d %H:%M:%S") > timedelta(days=SC4MP_SERVER_EXPIRY):
										continue
									self.new_servers.setdefault(server_id, entry)

								for server_id in self.new_servers.keys():
									if "stats" not in self.new_servers[server_id].keys():
										try:
											self.new_servers[server_id]["stats"] = self.servers[server_id]["stats"]
										except Exception:
											pass

								self.servers = self.new_servers
								self.new_servers = dict()
								self.server_queue = SC4MP_SERVERS.copy()
								tried_servers = []

								time.sleep(60) #300

								break

				except Exception as e:

					show_error(e)

					time.sleep(10)

		except KeyboardInterrupt:

			pass


	class Fetcher(Thread):


		def __init__(self, parent, server):

			super().__init__()

			self.parent = parent
			self.server = server


		def run(self):

			print(f"Fetching server at {self.server[0]}:{self.server[1]}...")

			try:

				try:

					entry = dict()

					entry["host"] = self.server[0]
					entry["port"] = self.server[1]
					entry["url"] = f"sc4mp://{entry['host']}:{entry['port']}"

					# Determine which protocol to use
					use_legacy = False
					try:
						server_id, server_version = self.fetch()
					except (NetworkException, ConnectionClosedException):
						use_legacy = True

					# Use legacy protocol if needed
					if use_legacy:
						server_id = self.get("server_id")
						server_version = self.get("server_version")

					entry["version"] = server_version
					entry["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

					# Only add server if we got a valid server_id
					if not server_id:
						print(f"[WARNING] Server at {self.server[0]}:{self.server[1]} failed to get server ID, skipping.")
						return

					entry["server_id"] = server_id	# for the region map URLs

					self.parent.new_servers.setdefault(server_id, entry)

					# Fetch server data using appropriate protocol
					if use_legacy:
						self.server_list_0_8()
						entry["info"] = self.server_info_0_8()
						if not entry["info"]["private"]:
							entry["stats"] = self.server_stats_0_8(server_id)
					else:
						self.server_list()
						entry["info"] = self.server_info()
						if not entry["info"]["private"]:
							entry["stats"] = self.server_stats(server_id)

				except TimeoutError:

					print(f"[WARNING] Server at {self.server[0]}:{self.server[1]} timed out.")

				except Exception as e:

					print(f"[WARNING] Failed to fetch server at {self.server[0]}:{self.server[1]}: {e}")

				finally:

					self.parent.thread_count -= 1

			except KeyboardInterrupt:

				pass


		def client_socket(self, timeout=30):
			"""Create a ClientSocket instance"""
			return ClientSocket(address=self.server, timeout=timeout)


		def socket_0_8(self):
			"""Create a regular socket for v0.8/v0.4 protocol"""
			s = socket()
			s.settimeout(30)
			s.connect(self.server)
			return s


		def get(self, request):
			"""Simple request/response for v0.8/v0.4 protocol"""
			s = self.socket_0_8()

			s.send(request.encode())

			return s.recv(SC4MP_BUFFER_SIZE).decode()


		# ===== SHARED HELPER METHODS =====

		def _load_json(self, filename):
			"""Returns data from a json file as a dictionary."""
			try:
				with open(filename, 'r') as file:
					data = json.load(file)
					if data is None:
						return dict()
					else:
						return data
			except FileNotFoundError:
				return dict()


		def _calculate_region_stats(self, temp_dir, server_time):
			"""Calculate region statistics from downloaded region data."""
			regions_path = os.path.join(temp_dir, "Regions")

			# Check if Regions directory exists (may not if no files were downloaded)
			if not os.path.exists(regions_path):
				return {
					"stat_mayors": 0,
					"stat_mayors_online": 0,
					"stat_claimed": 0
				}

			mayors = set()
			mayors_online = set()
			claimed_area = 0
			total_area = 0

			for region in os.listdir(regions_path):
				try:
					region_path = os.path.join(regions_path, region)
					region_config_path = os.path.join(region_path, "config.bmp")
					region_dimensions = get_bitmap_dimensions(region_config_path)
					region_database_path = os.path.join(region_path, "_Database", "region.json")
					region_database = self._load_json(region_database_path)

					for coords in region_database.keys():
						city_entry = region_database[coords]
						if city_entry is not None:
							owner = city_entry["owner"]
							if owner is not None:
								claimed_area += city_entry["size"] ** 2
								mayors.add(owner)
								modified = city_entry["modified"]
								if modified is not None:
									modified = datetime.strptime(modified, "%Y-%m-%d %H:%M:%S")
									if modified > server_time - timedelta(minutes=60):
										mayors_online.add(owner)
					total_area += region_dimensions[0] * region_dimensions[1]
				except Exception:
					# Skip directories that don't have the expected region structure
					pass

			stat_mayors = len(mayors)
			stat_mayors_online = len(mayors_online)

			try:
				stat_claimed = float(claimed_area) / float(total_area)
			except ZeroDivisionError:
				stat_claimed = 1

			return {
				"stat_mayors": stat_mayors,
				"stat_mayors_online": stat_mayors_online,
				"stat_claimed": stat_claimed
			}


		def _record_region_cities(self, temp_dir, server_id):
			"""Keeps the public stats of each city in the downloaded regions' databases, by savegame MD5, for the region maps."""

			FIELDS = ["city_name", "mayor_name", "population", "residential_population", "commercial_population", "industrial_population", "mayor_rating", "difficulty", "total_funds", "modified"]

			regions_path = os.path.join(temp_dir, "Regions")
			if not os.path.exists(regions_path):
				return

			regions = {}
			tiles = {}
			for region in os.listdir(regions_path):
				try:
					tiles[region] = read_region_config(os.path.join(regions_path, region, "config.bmp"))
				except Exception:
					pass
				region_database = self._load_json(os.path.join(regions_path, region, "_Database", "region.json"))
				cities = {}
				for city_entry in region_database.values():
					if isinstance(city_entry, dict) and is_md5(city_entry.get("hashcode")):
						city = {field: city_entry.get(field) for field in FIELDS}
						city["claimed"] = city_entry.get("owner") is not None
						cities[city_entry["hashcode"]] = city
				regions[region] = cities

			self.parent.region_cities[server_id] = regions
			self.parent.region_tiles[server_id] = tiles


		def _prune_file_table(self, target, file_table, server_id):
			"""Keeps the files needed for the stats (`region.json` and `config.bmp`), and the savegames not in the city cache yet, for the region maps (up to `SC4MP_MAP_DOWNLOAD_LIMIT` bytes per scan). Records the savegames in each region."""

			ft = []
			regions = {}
			new_size = 0

			for entry in file_table:
				checksum, filesize, relpath = entry[0], entry[1], str(entry[2]).replace("\\", "/")
				parts = relpath.split("/")
				if parts[-1] in ["region.json", "config.bmp"]:
					ft.append(entry)
				elif target == "regions" and len(parts) == 2 and parts[1].lower().endswith(".sc4") and is_md5(checksum):
					regions.setdefault(parts[0], []).append(checksum)
					if not city_cached(checksum) and new_size + filesize <= SC4MP_MAP_DOWNLOAD_LIMIT:
						ft.append(entry)
						new_size += filesize

			if target == "regions":
				self.parent.region_maps[server_id] = regions

			return ft


		def _cache_city(self, path, checksum, filesize):

			if str(path).lower().endswith(".sc4") and is_md5(checksum) and Path(path).stat().st_size == filesize:	# only complete downloads
				try:
					cache_city(path, checksum)
				except Exception as e:
					show_error(e)


		# ===== PROTOCOL METHODS =====

		def fetch(self):
			"""Fetch server ID and version"""
			s = self.client_socket()
			info = s.info()
			return info.get("server_id"), info.get("server_version")


		def server_list(self):
			"""Fetch server list"""
			s = self.client_socket()

			servers = s.server_list()

			# Loop through server list and append them to the unfetched servers
			for host, port in servers:
				self.parent.server_queue.append((host, port))


		def server_info(self):
			"""Fetch server info"""
			s = self.client_socket()

			return s.info()


		def server_stats(self, server_id):
			"""Calculate server stats"""

			def fetch_temp():

				# Create temporary directory
				temp_dir = tempfile.mkdtemp(prefix="sc4mp_api_")

				TARGETS = ["plugins", "regions"]
				DIRECTORIES = ["Plugins", "Regions"]

				total_size = 0

				for target, directory in zip(TARGETS, DIRECTORIES):

					# Set destination
					destination = os.path.join(temp_dir, directory)

					# Create the socket
					s = self.client_socket()

					# Request file table
					file_table = s.file_table(target)

					# Get total download size
					size = sum([entry[1] for entry in file_table])

					# Prune file table as necessary
					file_table = self._prune_file_table(target, file_table, server_id)

					# Download files
					for checksum, filesize, relpath, file_data in s.file_table_data(target, file_table):

						# Set the destination
						d = Path(destination) / relpath

						# Create the destination directory if necessary
						d.parent.mkdir(parents=True, exist_ok=True)

						# Delete the destination file if it exists
						d.unlink(missing_ok=True)

						# Receive the file
						with d.open("wb") as dest:
							for chunk in file_data:
								dest.write(chunk)

						# Keep savegames' region view images for the region maps
						self._cache_city(d, checksum, filesize)

					total_size += size

				return total_size, temp_dir


			def get_time():

				try:

					s = self.client_socket()
					return s.time()

				except Exception as e:

					show_error(e)

					return datetime.now()

			# Download files
			stat_download, temp_dir = fetch_temp()

			# Get server time
			server_time = get_time()

			# Calculate region statistics
			region_stats = self._calculate_region_stats(temp_dir, server_time)

			# Keep each city's stats, for the region maps
			self._record_region_cities(temp_dir, server_id)

			# Build entry
			entry = {
				"stat_mayors": region_stats["stat_mayors"],
				"stat_mayors_online": region_stats["stat_mayors_online"],
				"stat_claimed": region_stats["stat_claimed"],
				"stat_download": stat_download
			}

			# Cleanup temp files
			try:
				shutil.rmtree(temp_dir)
			except Exception as e:
				pass

			return entry


		# ===== V0.8/V0.4 PROTOCOL METHODS (LEGACY) =====

		def server_list_0_8(self):
			"""Fetch server list using v0.8/v0.4 protocol"""
			# Create socket
			s = self.socket_0_8()

			# Request server list
			s.send(b"server_list")

			# Receive server list
			servers = recv_json(s)

			# Loop through server list and append them to the unfetched servers
			for host, port in servers:
				self.parent.server_queue.append((host, port))


		def server_info_0_8(self):
			"""Fetch server info using v0.8/v0.4 protocol"""
			s = self.socket_0_8()

			s.send(b"info")

			return recv_json(s)


		def server_stats_0_8(self, server_id):
			"""Calculate server stats using v0.8/v0.4 protocol"""

			def fetch_temp():

				# Create temporary directory
				temp_dir = tempfile.mkdtemp(prefix="sc4mp_api_")

				REQUESTS = ["plugins", "regions"]
				DIRECTORIES = ["Plugins", "Regions"]

				total_size = 0

				for request, directory in zip(REQUESTS, DIRECTORIES):

					# Set destination
					destination = os.path.join(temp_dir, directory)

					# Create the socket
					s = self.socket_0_8()

					# Request the type of data
					s.send(request.encode())

					# Receive file table
					file_table = recv_json(s)

					# Get total download size
					size = sum([entry[1] for entry in file_table])

					# Prune file table as necessary
					file_table = self._prune_file_table(request, file_table, server_id)

					# Send pruned file table
					send_json(s, file_table)

					# Receive files
					for entry in file_table:

						# Get necessary values from entry
						filesize = entry[1]
						relpath = Path(entry[2])

						# Set the destination
						d = Path(destination) / relpath

						# Create the destination directory if necessary
						d.parent.mkdir(parents=True, exist_ok=True)

						# Delete the destination file if it exists
						d.unlink(missing_ok=True)

						# Receive the file
						filesize_read = 0
						with d.open("wb") as dest:
							while filesize_read < filesize:
								filesize_remaining = filesize - filesize_read
								buffersize = SC4MP_BUFFER_SIZE if filesize_remaining > SC4MP_BUFFER_SIZE else filesize_remaining
								bytes_read = s.recv(buffersize)
								if not bytes_read:
									break
								dest.write(bytes_read)
								filesize_read += len(bytes_read)

						# Keep savegames' region view images for the region maps
						self._cache_city(d, entry[0], filesize)

					total_size += size

				return total_size, temp_dir


			def get_time():


				try:

					s = self.socket_0_8()
					s.send(b"time")

					return datetime.strptime(s.recv(SC4MP_BUFFER_SIZE).decode(), "%Y-%m-%d %H:%M:%S")

				except Exception as e:

					show_error(e)

					return datetime.now()

			# Download files
			stat_download, temp_dir = fetch_temp()

			# Get server time
			server_time = get_time()

			# Calculate region statistics
			region_stats = self._calculate_region_stats(temp_dir, server_time)

			# Keep each city's stats, for the region maps
			self._record_region_cities(temp_dir, server_id)

			# Build entry
			entry = {
				"stat_mayors": region_stats["stat_mayors"],
				"stat_mayors_online": region_stats["stat_mayors_online"],
				"stat_claimed": region_stats["stat_claimed"],
				"stat_download": stat_download
			}

			# Cleanup temp files
			try:
				shutil.rmtree(temp_dir)
			except Exception as e:
				pass

			return entry


class Logger():


	def __init__(self):


		self.terminal = sys.stdout
		self.log = "sc4mpapi.log"

		try:
			unlink(self.log)
		except Exception:
			pass


	def write(self, message):


		output = message

		if (message != "\n"):

			# Timestamp
			timestamp = datetime.now().strftime("[%H:%M:%S] ")

			# Label
			label = "[SC4MP/" + current_thread().getName() + "] "
			for item in stack()[1:]:
				try:
					label += "(" + item[0].f_locals["self"].__class__.__name__ + ") "
					break
				except Exception:
					pass


			# Type and color
			type = "[INFO] "
			color = '\033[90m '
			TYPES_COLORS = [
				("[INFO] ", '\033[90m '), #'\033[94m '
				("[PROMPT]", '\033[01m '),
				("[WARNING] ", '\033[93m '),
				("[ERROR] ", '\033[91m '),
				("[FATAL] ", '\033[91m ')
			]
			for index in range(len(TYPES_COLORS)):
				current_type = TYPES_COLORS[index][0]
				current_color = TYPES_COLORS[index][1]
				if (message[:len(current_type)] == current_type):
					message = message[len(current_type):]
					type = current_type
					color = current_color
					break
			if (current_thread().getName() == "Main" and type == "[INFO] "):
				color = '\033[00m '

			# Assemble
			output = color + timestamp + label + type + message

		# Print
		self.terminal.write(output)
		with open(self.log, "a") as log:
			log.write(output)
			log.close()


	def flush(self):

		self.terminal.flush()


init()

if __name__ == "__main__":
    main()
