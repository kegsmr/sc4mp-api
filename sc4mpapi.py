import json
import os
import shutil
import struct
import sys
import random
import traceback
import time
#import ssl
from argparse import ArgumentParser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from inspect import stack
from os import unlink
from pathlib import Path
from socket import socket
from threading import Thread, current_thread


SC4MP_TITLE = "SC4MP API"

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]

SC4MP_BUFFER_SIZE = 4096


def main():

	args = parse_args()

	sys.stdout = Logger()
	current_thread().name = "Main"

	print(SC4MP_TITLE)

	print("Starting scanner...")

	global sc4mp_scanner
	sc4mp_scanner = Scanner()
	sc4mp_scanner.start()

	print("Starting webserver...")

	webserver = HTTPServer((args.host, int(args.port)), RequestHandler)
	#webserver.socket = ssl.wrap_socket (webserver.socket, keyfile="key.pem", certfile='cert.pem', server_side=True)

	print("Webserver started on http://%s:%s" % (args.host, args.port))

	try:
		webserver.serve_forever()
	except KeyboardInterrupt:
		pass

	webserver.server_close()

	print("Webserver stopped.")

	print("Stopping scanner...")

	sc4mp_scanner.end = True


def parse_args():

	parser = ArgumentParser()

	parser.add_argument("host")
	parser.add_argument("port")
	
	return parser.parse_args()


def get_bitmap_dimensions(filename):

	with open(filename, "rb") as file:
		data = bytearray(file.read())

	width = struct.unpack_from('<i', data, 18)
	height = struct.unpack_from('<i', data, 22)

	return (width[0], height[0])


"""def send_json(s, data):
	
	s.sendall(json.dumps(data).encode())"""


"""def recv_json(s):
	
	data = ""
	while True:
		data += s.recv(SC4MP_BUFFER_SIZE).decode()
		try:
			return json.loads(data)
		except json.decoder.JSONDecodeError:
			pass"""


def show_error(e):
	
	message = None
	if isinstance(e, str):
		message = e
	else: 
		message = str(e)

	print("[ERROR] " + message + "\n\n" + traceback.format_exc())


class Scanner(Thread):


	def __init__(self):

		super().__init__()

		self.MAX_THREADS = 50

		try:
			shutil.rmtree(os.path.join("_SC4MP", "_Temp", "ServerList"))
		except Exception as e:
			show_error(e)

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

						while True:

							if len(self.server_queue) > 0:

								break

							elif count < 60 and self.thread_count > 0:

								count += 1

								time.sleep(10)
							
							else:

								for server_id in self.new_servers.keys():
									if "stats" not in self.new_servers[server_id].keys():
										try:
											self.new_servers[server_id]["stats"] = self.servers[server_id]["stats"]
										except:
											pass

								self.servers = self.new_servers
								self.new_servers = dict()
								self.server_queue = SC4MP_SERVERS.copy()
								tried_servers = []

								time.sleep(300) #60

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

					server_id = self.get("server_id")
					server_version = self.get("server_version")

					entry["version"] = server_version
					entry["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

					self.parent.new_servers.setdefault(server_id, entry)

					if (server_version[:3] == "0.3"):
						self.server_list_3()
						entry["info"] = self.server_info_3()
						if not entry["info"]["private"]:
							entry["stats"] = self.server_stats_3(server_id)
					else:
						self.server_list()
						entry["info"] = self.server_info()
						if not entry["info"]["private"]:
							entry["stats"] = self.server_stats(server_id)	

				except Exception as e:

					show_error(e)

				self.parent.thread_count -= 1

			except KeyboardInterrupt:

				pass


		def socket(self):

			s = socket()

			s.settimeout(10)

			s.connect(self.server)

			return s


		def get(self, request):

			s = self.socket()

			s.send(request.encode())

			return s.recv(SC4MP_BUFFER_SIZE).decode()
		

		def server_list_3(self):
			s = self.socket()
			s.send(b"server_list")
			size = int(s.recv(SC4MP_BUFFER_SIZE).decode())
			s.send(b"ok")
			for count in range(size):
				host = s.recv(SC4MP_BUFFER_SIZE).decode()
				s.send(b"ok")
				port = int(s.recv(SC4MP_BUFFER_SIZE).decode())
				s.send(b"ok")
				self.parent.server_queue.append((host, port))
		

		def server_info_3(self):

			entry = dict()
			
			entry["server_id"] = self.get("server_id")
			entry["server_name"] = self.get("server_name")
			entry["server_description"] = self.get("server_description")
			entry["server_url"] = self.get("server_url")
			entry["server_version"] = self.get("server_version")
			entry["private"] = self.get("private") == "yes"
			entry["password_enabled"] = self.get("password_enabled") == "yes"
			entry["user_plugins_enabled"] = self.get("user_plugins_enabled") == "yes"
			
			return entry
		

		def server_stats_3(self, server_id):


			def load_json(filename):
				"""Returns data from a json file as a dictionary."""
				try:
					with open(filename, 'r') as file:
						data = json.load(file)
						if data == None:
							return dict()
						else:
							return data
				except FileNotFoundError:
					return dict()


			def fetch_temp():


				def receive_or_cached(s, rootpath):

					# Receive hashcode and set cache filename
					hash = s.recv(SC4MP_BUFFER_SIZE).decode()

					# Separator
					s.send(b"ok")

					# Receive filesize
					filesize = int(s.recv(SC4MP_BUFFER_SIZE).decode())

					# Separator
					s.send(b"ok")

					# Receive relative path and set the destination
					relpath = os.path.normpath(s.recv(SC4MP_BUFFER_SIZE).decode())
					filename = os.path.split(relpath)[1]
					destination = os.path.join(rootpath, relpath)

					if not (filename == "region.json" or filename == "config.bmp"):

						# Tell the server that the file is cached
						s.send(b"cached")

					else:

						# Tell the server that the file is not cached
						s.send(b"not cached")

						# Create the destination directory if necessary
						destination_directory = os.path.split(destination)[0]
						if (not os.path.exists(destination_directory)):
							os.makedirs(destination_directory)

						# Delete the destination file if it exists
						if (os.path.exists(destination)):
							os.remove(destination)

						# Receive the file
						filesize_read = 0
						destination_file = open(destination, "wb")
						while (filesize_read < filesize):
							bytes_read = s.recv(SC4MP_BUFFER_SIZE)
							#if not bytes_read:    
							#	break
							destination_file.write(bytes_read)
							filesize_read += len(bytes_read)
						
					# Return the file size
					return filesize

				REQUESTS = [b"plugins", b"regions"]
				DIRECTORIES = ["Plugins", "Regions"]

				size_downloaded = 0

				for request, directory in zip(REQUESTS, DIRECTORIES):

					# Set destination
					destination = os.path.join("_SC4MP", "_Temp", "ServerList", server_id, directory)

					# Make destination directory if it does not exist
					if not os.path.exists(destination):
						os.makedirs(destination)

					# Create the socket
					s = socket()
					s.settimeout(30)
					s.connect(self.server)

					# Request the type of data
					s.send(request)

					# Receive file count
					file_count = int(s.recv(SC4MP_BUFFER_SIZE).decode())

					# Separator
					s.send(b"ok")

					# Receive file size
					size = int(s.recv(SC4MP_BUFFER_SIZE).decode())

					# Receive files
					for files_received in range(file_count):
						s.send(b"ok")
						size_downloaded += receive_or_cached(s, destination)

				return size_downloaded


			entry = dict()

			download = fetch_temp()

			regions_path = os.path.join("_SC4MP", "_Temp", "ServerList", server_id, "Regions")

			mayors = []
			mayors_online = []
			claimed_area = 0
			total_area = 0
			for region in os.listdir(regions_path):
				try:
					region_path = os.path.join(regions_path, region)
					region_config_path = os.path.join(region_path, "config.bmp")
					region_dimensions = get_bitmap_dimensions(region_config_path)
					region_database_path = os.path.join(region_path, "_Database", "region.json")
					region_database = load_json(region_database_path)
					for coords in region_database.keys():
						city_entry = region_database[coords]
						if city_entry != None:
							owner = city_entry["owner"]
							if (owner != None):
								claimed_area += city_entry["size"] ** 2
								if (owner not in mayors):
									mayors.append(owner)
								modified = city_entry["modified"]
								if (modified != None):
									modified = datetime.strptime(modified, "%Y-%m-%d %H:%M:%S")
									if (modified > datetime.now() - timedelta(hours=26) and owner not in mayors_online):
										mayors_online.append(owner)
					total_area += region_dimensions[0] * region_dimensions[1]
				except Exception as e:
					show_error(e) #pass

			stat_mayors = len(mayors) #(random.randint(0,1000))
			
			stat_mayors_online = len(mayors_online)
			
			try:
				stat_claimed = (float(claimed_area) / float(total_area)) #(float(random.randint(0, 100)) / 100)
			except ZeroDivisionError:
				stat_claimed = 1

			stat_download = download #(random.randint(0, 10 ** 11))

			entry["stat_mayors"] = stat_mayors
			entry["stat_mayors_online"] = stat_mayors_online
			entry["stat_claimed"] = stat_claimed
			entry["stat_download"] = stat_download

			try:
				shutil.rmtree(os.path.join("_SC4MP", "_Temp", "ServerList", server_id))
			except Exception as e:
				show_error(e)

			return entry


		def server_list(self):

			# Create socket
			s = self.socket()
			
			# Request server list
			s.send(b"server_list")
			
			# Receive server list
			servers = recv_json(s)

			# Loop through server list and append them to the unfetched servers
			for host, port in servers:
				self.parent.server_queue.append((host, port))
		

		def server_info(self):

			return json.loads(self.get("info"))


		def server_stats(self, server_id):


			def load_json(filename):
				"""Returns data from a json file as a dictionary."""
				try:
					with open(filename, 'r') as file:
						data = json.load(file)
						if data == None:
							return dict()
						else:
							return data
				except FileNotFoundError:
					return dict()


			def fetch_temp():
				

				REQUESTS = ["plugins", "regions"]
				DIRECTORIES = ["Plugins", "Regions"]

				total_size = 0

				for request, directory in zip(REQUESTS, DIRECTORIES):

					# Set destination
					destination = os.path.join("_SC4MP", "_Temp", "ServerList", server_id, directory)

					# Create the socket
					s = self.socket()

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

				return total_size
				

			def time():
				

				try:

					s = socket()
					s.settimeout(10)
					s.connect((self.server[0], self.server[1]))
					s.send(b"time")

					return datetime.strptime(s.recv(SC4MP_BUFFER_SIZE).decode(), "%Y-%m-%d %H:%M:%S")
				
				except Exception as e:

					show_error(e)

					return datetime.now()


			entry = dict()

			download = fetch_temp()

			regions_path = os.path.join("_SC4MP", "_Temp", "ServerList", server_id, "Regions")

			server_time = time()

			mayors = []
			mayors_online = []
			claimed_area = 0
			total_area = 0
			for region in os.listdir(regions_path):
				try:
					region_path = os.path.join(regions_path, region)
					region_config_path = os.path.join(region_path, "config.bmp")
					region_dimensions = get_bitmap_dimensions(region_config_path)
					region_database_path = os.path.join(region_path, "_Database", "region.json")
					region_database = load_json(region_database_path)
					for coords in region_database.keys():
						city_entry = region_database[coords]
						if city_entry != None:
							owner = city_entry["owner"]
							if (owner != None):
								claimed_area += city_entry["size"] ** 2
								if (owner not in mayors):
									mayors.append(owner)
								modified = city_entry["modified"]
								if (modified != None):
									modified = datetime.strptime(modified, "%Y-%m-%d %H:%M:%S")
									if (modified > server_time - timedelta(minutes=60) and owner not in mayors_online):
										mayors_online.append(owner)
					total_area += region_dimensions[0] * region_dimensions[1]
				except Exception as e:
					show_error(e) #pass

			stat_mayors = len(mayors) #(random.randint(0,1000))
			
			stat_mayors_online = len(mayors_online)
			
			try:
				stat_claimed = (float(claimed_area) / float(total_area)) #(float(random.randint(0, 100)) / 100)
			except ZeroDivisionError:
				stat_claimed = 1

			stat_download = download #(random.randint(0, 10 ** 11))

			entry["stat_mayors"] = stat_mayors
			entry["stat_mayors_online"] = stat_mayors_online
			entry["stat_claimed"] = stat_claimed
			entry["stat_download"] = stat_download

			try:
				shutil.rmtree(os.path.join("_SC4MP", "_Temp", "ServerList", server_id))
			except Exception as e:
				show_error(e)

			return entry


class RequestHandler(BaseHTTPRequestHandler):
    
	def do_GET(self):
		path = self.path.split("/")
		while "" in path:
			path.remove("")
		if (path == ["servers"]):
			self.send_json(list(sc4mp_scanner.servers.values()))
		elif (path == ["example-servers"]):
			self.send_response(200)
			servers = []
			for i in range(100):
				entry = {}
				entry["host"] = "255.255.255.255"
				entry["port"] = "7240"
				entry["info"] = {
					"server_name": "XXXXXXXXXXXXXXXXXXXXXX",
					"password_enabled": random.choice([True, False])
				}
				mayors = random.randint(0, 10000)
				online = random.randint(0, mayors)
				claimed = random.uniform(0, 1)
				download = random.randint(0, 10000000000)
				entry["stats"] = {
					"stat_mayors": mayors,
					"stat_mayors_online": online,
					"stat_claimed": claimed,
					"stat_download": download,
				}
				servers.append(entry)
			self.send_json(servers)
		else:
			self.send_error(404)

	def send_json(self, data):
		self.send_response(200)
		self.send_header("Content-type", "application/json")
		self.send_header("Access-Control-Allow-Origin", "*")
		self.end_headers()
		self.wfile.write(json.dumps(data, indent=4).encode())


class Logger():
	
	

	def __init__(self):
		

		self.terminal = sys.stdout
		self.log = "sc4mpapi.log"
		
		try:
			unlink(self.log)
		except:
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
				except:
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


if __name__ == "__main__":        
    main()