import json
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import traceback
from argparse import ArgumentParser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from inspect import stack
from os import unlink
from pathlib import Path
from socket import socket
from threading import Thread, current_thread

try:
	from flask import Flask, jsonify, abort, send_from_directory
	app = Flask(__name__)
	sc4mp_has_flask = True
except ImportError:
	sc4mp_has_flask = False

from core.networking import \
	ClientSocket, NetworkException, ConnectionClosedException, \
	send_json, recv_json, BUFFER_SIZE


SC4MP_TITLE = "SC4MP API"

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]

SC4MP_BUFFER_SIZE = BUFFER_SIZE


def init():

	global sc4mp_scanner

	sys.stdout = Logger()
	current_thread().name = "Main"

	print(SC4MP_TITLE)

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


class Scanner(Thread):


	def __init__(self):

		super().__init__()
		self.daemon = True

		self.MAX_THREADS = 50

		self.new_servers = dict()
		self.servers = self.new_servers
		self.server_queue = SC4MP_SERVERS.copy()
		self.thread_count = 0
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
					ft = []
					for entry in file_table:
						filename = Path(entry[2]).name
						if filename in ["region.json", "config.bmp"]:
							ft.append(entry)
					file_table = ft

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
					ft = []
					for entry in file_table:
						filename = Path(entry[2]).name
						if filename in ["region.json", "config.bmp"]:
							ft.append(entry)
					file_table = ft

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
