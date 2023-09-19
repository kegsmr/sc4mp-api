from datetime import datetime
import json
import time
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import socket
from threading import Thread

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]

SC4MP_BUFFER_SIZE = 4096


def main():

	args = parse_args()

	print("Starting scanner...")

	global sc4mp_scanner
	sc4mp_scanner = Scanner()
	sc4mp_scanner.start()

	print("Starting webserver...")

	webserver = HTTPServer((args.host, int(args.port)), RequestHandler)

	print("Webserver started on http://%s:%s" % (args.host, args.port))

	try:
		webserver.serve_forever()
	except KeyboardInterrupt:
		pass

	webserver.server_close()

	print("Webserver stopped.")

	print("Ending scanner...")

	sc4mp_scanner.end = True


def parse_args():

	parser = ArgumentParser()

	parser.add_argument("host")
	parser.add_argument("port")
	
	return parser.parse_args()


class Scanner(Thread):


	def __init__(self):

		super().__init__()

		self.MAX_THREADS = 50

		self.new_servers = dict()
		self.servers = self.new_servers
		self.server_queue = SC4MP_SERVERS.copy()
		self.thread_count = 0
		self.end = False

	
	def run(self):

		tried_servers = []

		while not self.end:

			try:

				if len(self.server_queue) > 0:

					if self.thread_count < self.MAX_THREADS:

						server = self.server_queue.pop(0)

						if not server in tried_servers:

							print(f"Fetching server at {server[0]}:{server[1]}...")

							fetcher = self.Fetcher(self, server)
							fetcher.start()

							self.thread_count += 1

							tried_servers.append(server)
					
					time.sleep(.1)

				else:

					if self.thread_count > 0:
						time.sleep(10)

					if not len(self.server_queue) > 0:

						self.servers = self.new_servers
						self.new_servers = dict()
						self.server_queue = SC4MP_SERVERS.copy()
						tried_servers = []

						time.sleep(60)


			except Exception as e:

				print("ERROR: " + str(e))

				time.sleep(10)	


	class Fetcher(Thread):

		
		def __init__(self, parent, server):

			super().__init__()

			self.parent = parent
			self.server = server

		
		def run(self):

			try:

				entry = dict()

				entry["host"] = self.server[0]
				entry["port"] = self.server[1]

				server_id = self.get("server_id")

				server_version = self.get("server_version")

				if (server_version[:3] == "0.3"):
					self.server_list_3()

				entry["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

				self.parent.new_servers.setdefault(server_id, entry)

			except Exception as e:

				print(f"ERROR: {e}")

			self.parent.thread_count -= 1


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
		

class RequestHandler(BaseHTTPRequestHandler):
    
	def do_GET(self):
		path = self.path.split("/")
		if (path[1] == "servers"):
			self.send_response(200)
			self.send_header("Content-type", "application/json")
			self.end_headers()
			self.wfile.write(json.dumps(sc4mp_scanner.servers, indent=4).encode())
		else:
			self.send_error(404)


if __name__ == "__main__":        
    main()