from tornado import netutil, ioloop, iostream, httpclient
from functools import partial
import socket
import ctypes
import os
import traceback
import re
import socket_error

libc = ctypes.cdll.LoadLibrary('libc.so.6')
splice_syscall = libc.splice

SPLICE_F_NONBLOCK = 0x02
SPLICE_F_MOVE = 0x01

import resource
chunk_size = resource.getpagesize()

header = 'GET /'

error_response = 'HTTP/1.1 400 Internal Error\r\nServer: Bogus 0\r\nConnection: Close\r\n\r\nInvalid Request'
not_found_response = 'HTTP/1.1 404 Not Found\r\nConnection: Close\r\n\r\nNot Found'

paths = {'favicon.ico':not_found_response, '':not_found_response}

def const(c):
    def res(*args):
        return c
    return res

valid_headers = re.compile('^(User-Agent|Connection|Accept.*):')

amount_read = 0
amount_written = 0

host = socket.gethostname()
mixpanel_token = '7fb5000c304c26e32ed1b6744cea1ddd'

def noop(*args):
    return

errno_loc = libc.__errno_location
errno_loc.restype = ctypes.POINTER(ctypes.c_int)

def get_errno():
    return errno_loc().contents.value

def handle_close(stream, innerfunc=None):
    def callback():
        if stream.error:
            print stream.error
        if innerfunc:
            innerfunc()
    stream.set_close_callback(callback)

def splice(left, right):
    total = 0

    while 1:
        code = splice_syscall(left, 0, right, 0, chunk_size, SPLICE_F_NONBLOCK | SPLICE_F_MOVE)

        print code

        if code == -1:
            socket_error.raise_socket_error(get_errno())

        total += code

        if code < chunk_size:
            break

    return total

import json, time, base64
def track_throughput():
    global amount_written
    data = json.dumps({
        'event':'proxy_throughput',
        'properties':{
            'amount':amount_written,
            'time':int(time.time()),
            'token':mixpanel_token,
            'host':host}})
    client = httpclient.AsyncHTTPClient()
    client.fetch('http://api.mixpanel.com/track/?data=%s' % base64.b64encode(data), noop)
    amount_written = 0

mixpanel_tracker = ioloop.PeriodicCallback(track_throughput, 1800000)
mixpanel_tracker.start()

class Request(object):

    def __init__(self, stream, address):
        self.left = stream
        self.source_address = address
        self.right = None
        self.prefix = None

        self.right_ready = False

        read, write = os.pipe()

        self.pipe_read = read
        self.pipe_write = write

        self.left.read_bytes(len(header), self.handle_body)

    def handle_body(self, data):
        if data != header:
            self.left.write(error_response, stream.close)
            print repr(data)
            return
        self.prefix = data
        self.left.read_until_regex(r'[ /]', self.handle_host)

    def handle_host(self, host):
        if host[-1] == '/':
            host = host[:-1]
        host = re.sub(r'[^a-zA-Z0-9\.\-\_]', '', host)
        print repr(host)
        self.host = host
        if host in paths:
            self.left.write(paths[host], self.left.close)
        else:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.right = iostream.IOStream(sock)
                handle_close(self.right, self.left.close)
                handle_close(self.left, self.right.close)
                self.right.connect((socket.gethostbyname(host), 80), self.backend_connected)
            except:
                traceback.print_exc()
                self.left.write(not_found_response, self.left.close)
                self.right.close()

    def backend_connected(self):
        print 'backend connected'
        print repr(self.prefix)
        self.right.write(self.prefix, self.get_header)

    def get_header(self, data=None):
        if data == '\r\n':
            self.right.write('Host: %s\r\n' % self.host)
            self.right.write(data)
            self.right.read_until('\r\n\r\n', self.proxy_headers)
        else:
            if data and (':' not in data or valid_headers.match(data)):
                self.right.write(data)
            self.left.read_until('\r\n', self.get_header)

    def proxy_headers(self, data):
        print repr(data)
        self.left.write(data[:-2])
        self.left.write('Access-Control-Allow-Origin: *\r\n\r\n', self.start)

    def start(self):
        print 'started'
        self.right.writing = const(True)
        self.right._handle_write = self.set_right_ready
        self.left.reading = const(True)
        self.left._handle_read = self.set_left_ready

    def set_right_ready(self):
        global amount_read
        if not self.right_ready:
            print 'right ready'
            try:
                amount_read += splice(self.right.socket.fileno(), self.pipe_write)
            except socket_error.EAGAIN:
                return
            except:
                self.left.close()
                self.right.close()
                traceback.print_exc()
                return
        self.right_ready = True

    def set_left_ready(self):
        global amount_written
        if self.right_ready:
            print 'shunt'
            try:
                amount_written += splice(self.pipe_read, self.left.socket.fileno())
            except socket_error.EAGAIN:
                pass
            except:
                self.left.close()
                self.right.close()
                traceback.print_exc()
                return
            self.right_ready = False

    def __del__(self):
        os.close(self.pipe_read)
        os.close(self.pipe_write)


class Server(netutil.TCPServer):

    def handle_stream(self, stream, address):
        Request(stream, address)

if __name__ == '__main__':
    server = Server()
    server.listen(os.environ.get('PORT', 5000))
    ioloop.IOLoop.instance().start()

