import json
import time
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import socket
from threading import Thread

SC4MP_SERVERS = [("servers.sc4mp.org", port) for port in range(7240, 7250)]


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

		self.servers = dict()
		self.end = False

	
	def run(self):

		server_queue = []

		while not self.end:

			try:

				if len(server_queue) > 0:

					server = server_queue.pop(0)

					print(f"Fetching server at {server[0]}:{server[1]}...")

					try:
						server_id = self.request(server, ["server_id"]).decode()
					except:
						pass

					if len(server_id) > 0 and server_id not in self.servers:
						self.servers[server_id] = dict()
					else:
						print("- failed!")
						continue

					print(f"- adding {server_id} to server list...")

					self.servers[server_id]["host"] = server[0]
					self.servers[server_id]["port"] = server[1]

					print("- done")

				else:

					server_queue = SC4MP_SERVERS.copy()

				time.sleep(.1)

			except Exception as e:

				print("ERROR: " + str(e))

				time.sleep(1)	


	def request(self, server, args):

		s = socket()
		
		s.settimeout(5)
		
		s.connect((server[0], server[1]))	
		
		s.send(" ".join(args).encode())

		return s.recv(4096)


class RequestHandler(BaseHTTPRequestHandler):
    
	def do_GET(self):
		if (self.path == "/servers"):
			self.send_response(200)
			self.send_header("Content-type", "application/json")
			self.end_headers()
			self.wfile.write(json.dumps(sc4mp_scanner.servers, indent=4).encode())
		else:
			self.send_error(404)


if __name__ == "__main__":        
    main()