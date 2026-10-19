import hashlib
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

import regionmaps
from regionmaps import SC4MP_CITIES_PATH, SC4MP_MAP_WIDTH, is_md5, city_cached, cache_city, render_region_map


SC4MP_TITLE = "SC4MP API"

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]

SC4MP_BUFFER_SIZE = BUFFER_SIZE

SC4MP_SERVER_EXPIRY = 30 # days

# Region maps
SC4MP_MAPS_PATH = Path("_SC4MP") / "_Cache" / "Maps"		# assembled region maps
SC4MP_MAP_DOWNLOAD_LIMIT = 100_000_000	# bytes of new savegames downloaded per server per scan, so big servers fill in over a few scans
SC4MP_MAP_MAX_WIDTH = 4096


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

	if not regionmaps.sc4mp_has_pillow:
		abort(503)

	try:
		width = max(100, min(SC4MP_MAP_MAX_WIDTH, int(request.args.get("width", SC4MP_MAP_WIDTH))))
	except ValueError:
		abort(400)

	# Maps are cached until a city in the region changes
	ready = sorted(checksum for checksum in checksums if city_cached(checksum))
	key = hashlib.sha1(json.dumps([server_id, region, width]).encode()).hexdigest()[:16]
	version = hashlib.sha1(json.dumps(ready).encode()).hexdigest()[:16]
	path = SC4MP_MAPS_PATH / f"{key}-{version}.png"

	if not path.exists():
		image = render_region_map(ready, width)
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

	layout = regionmaps.region_map_layout([checksum for checksum in checksums if city_cached(checksum)])
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
				outline=[[round(x / width, 5), round(y / height, 5)] for x, y in regionmaps.city_outline(city)],
			)
			for city in reversed(cities)
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
			for region in os.listdir(regions_path):
				region_database = self._load_json(os.path.join(regions_path, region, "_Database", "region.json"))
				cities = {}
				for city_entry in region_database.values():
					if isinstance(city_entry, dict) and is_md5(city_entry.get("hashcode")):
						city = {field: city_entry.get(field) for field in FIELDS}
						city["claimed"] = city_entry.get("owner") is not None
						cities[city_entry["hashcode"]] = city
				regions[region] = cities

			self.parent.region_cities[server_id] = regions


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
