#!/usr/bin/env python
# SOCKSv5 proxy server for network-sandbox
# Copyright 2015 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import asyncio
import errno
import os
import socket
import struct
import sys


class Socks5Server(object):
	"""
	An asynchronous SOCKSv5 server.
	"""

	@asyncio.coroutine
	def handle_proxy_conn(self, reader, writer):
		"""
		Handle incoming client connection. Perform SOCKSv5 request
		exchange, open a proxied connection and start relaying.

		@param reader: Read side of the socket
		@type reader: asyncio.StreamReader
		@param writer: Write side of the socket
		@type writer: asyncio.StreamWriter
		"""

		try:
			# SOCKS hello
			data = yield from reader.readexactly(2)
			vers, method_no = struct.unpack('!BB', data)

			if vers != 0x05:
				# disconnect on invalid packet -- we have no clue how
				# to reply in alien :)
				writer.close()
				return

			# ...and auth method list
			data = yield from reader.readexactly(method_no)
			for method in data:
				if method == 0x00:
					break
			else:
				# no supported method
				method = 0xFF

			# auth reply
			repl = struct.pack('!BB', 0x05, method)
			writer.write(repl)
			yield from writer.drain()
			if method == 0xFF:
				writer.close()
				return

			# request
			data = yield from reader.readexactly(4)
			vers, cmd, rsv, atyp = struct.unpack('!BBBB', data)

			if vers != 0x05 or rsv != 0x00:
				# disconnect on malformed packet
				self.close()
				return

			# figure out if we can handle it
			rpl = 0x00
			if cmd != 0x01:  # CONNECT
				rpl = 0x07  # command not supported
			elif atyp == 0x01:  # IPv4
				data = yield from reader.readexactly(4)
				addr = socket.inet_ntoa(data)
			elif atyp == 0x03:  # domain name
				data = yield from reader.readexactly(1)
				addr_len, = struct.unpack('!B', data)
				addr = yield from reader.readexactly(addr_len)
			elif atyp == 0x04:  # IPv6
				data = yield from reader.readexactly(16)
				addr = socket.inet_ntop(socket.AF_INET6, data)
			else:
				rpl = 0x08  # address type not supported

			# try to connect if we can handle it
			if rpl == 0x00:
				data = yield from reader.readexactly(2)
				port, = struct.unpack('!H', data)

				try:
					# open a proxied connection
					proxied_reader, proxied_writer = yield from asyncio.open_connection(
							addr, port)
				except (socket.gaierror, socket.herror):
					# DNS failure
					rpl = 0x04  # host unreachable
				except OSError as e:
					# connection failure
					if e.errno in (errno.ENETUNREACH, errno.ENETDOWN):
						rpl = 0x03  # network unreachable
					elif e.errno in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
						rpl = 0x04  # host unreachable
					elif e.errno in (errno.ECONNREFUSED, errno.ETIMEDOUT):
						rpl = 0x05  # connection refused
					else:
						raise
				else:
					# get socket details that we can send back to the client
					# local address (sockname) in particular -- but we need
					# to ask for the whole socket since Python's sockaddr
					# does not list the family...
					sock = proxied_writer.get_extra_info('socket')
					addr = sock.getsockname()
					if sock.family == socket.AF_INET:
						host, port = addr
						bin_host = socket.inet_aton(host)

						repl_addr = struct.pack('!B4sH',
								0x01, bin_host, port)
					elif sock.family == socket.AF_INET6:
						# discard flowinfo, scope_id
						host, port = addr[:2]
						bin_host = socket.inet_pton(sock.family, host)

						repl_addr = struct.pack('!B16sH',
								0x04, bin_host, port)

			if rpl != 0x00:
				# fallback to 0.0.0.0:0
				repl_addr = struct.pack('!BLH', 0x01, 0x00000000, 0x0000)

			# reply to the request
			repl = struct.pack('!BBB', 0x05, rpl, 0x00)
			writer.write(repl + repl_addr)
			yield from writer.drain()

			# close if an error occured
			if rpl != 0x00:
				writer.close()
				return

			# otherwise, start two loops:
			# remote -> local...
			t = asyncio.async(self.handle_proxied_conn(
					proxied_reader, writer, asyncio.Task.current_task()))

			# and local -> remote...
			try:
				try:
					while True:
						data = yield from reader.read(4096)
						if data == b'':
							# client disconnected, stop relaying from
							# remote host
							t.cancel()
							break

						proxied_writer.write(data)
						yield from proxied_writer.drain()
				except OSError:
					# read or write failure
					t.cancel()
				except:
					t.cancel()
					raise
			finally:
				# always disconnect in the end :)
				proxied_writer.close()
				writer.close()

		except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
			writer.close()
			return
		except:
			writer.close()
			raise

	@asyncio.coroutine
	def handle_proxied_conn(self, proxied_reader, writer, parent_task):
		"""
		Handle the proxied connection. Relay incoming data
		to the client.

		@param reader: Read side of the socket
		@type reader: asyncio.StreamReader
		@param writer: Write side of the socket
		@type writer: asyncio.StreamWriter
		"""

		try:
			try:
				while True:
					data = yield from proxied_reader.read(4096)
					if data == b'':
						break

					writer.write(data)
					yield from writer.drain()
			finally:
				parent_task.cancel()
		except (OSError, asyncio.CancelledError):
			return


if __name__ == '__main__':
	if len(sys.argv) != 2:
		print('Usage: %s <socket-path>' % sys.argv[0])
		sys.exit(1)

	loop = asyncio.get_event_loop()
	s = Socks5Server()
	server = loop.run_until_complete(
		asyncio.start_unix_server(s.handle_proxy_conn, sys.argv[1], loop=loop))

	ret = 0
	try:
		try:
			loop.run_forever()
		except KeyboardInterrupt:
			pass
		except:
			ret = 1
	finally:
		server.close()
		loop.run_until_complete(server.wait_closed())
		loop.close()
		os.unlink(sys.argv[1])
