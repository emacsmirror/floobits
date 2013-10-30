import sys
import socket
import select
import collections
import json
import errno
import os.path

try:
    import ssl
    assert ssl
except ImportError:
    ssl = False
try:
    from .. import editor, cert, msg, shared as G, utils, reactor
    from . import base
    assert cert and G and msg and utils
except (ImportError, ValueError):
    from floo import editor
    from floo.common import cert, msg, shared as G, utils, reactor
    import base

try:
    connect_errno = (errno.WSAEWOULDBLOCK, errno.WSAEALREADY, errno.WSAEINVAL)
    iscon_errno = errno.WSAEISCONN
except Exception:
    connect_errno = (errno.EINPROGRESS, errno.EALREADY)
    iscon_errno = errno.EISCONN


PY2 = sys.version_info < (3, 0)


def sock_debug(*args, **kwargs):
    if G.SOCK_DEBUG:
        msg.log(*args, **kwargs)


class FlooProtocol(base.BaseProtocol):
    ''' Base FD Interface'''
    NEWLINE = '\n'.encode('utf-8')
    MAX_RETRIES = 20
    INITIAL_RECONNECT_DELAY = 500

    def __init__(self, host, port, secure=True):
        super(FlooProtocol, self).__init__()

        self.host = host
        self.port = port
        self.secure = secure
        self.connected = False

        self._listener = False
        self._needs_handshake = bool(secure)
        self._sock = None
        self._q = collections.deque()
        self._buf = bytes()
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._retries = self.MAX_RETRIES
        self._empty_reads = 0
        self._reconnect_timeout = None
        self._cert_path = os.path.join(G.BASE_DIR, 'startssl-ca.pem')

    def _handle(self, data):
        self._buf += data
        while True:
            before, sep, after = self._buf.partition(self.NEWLINE)
            if not sep:
                return
            try:
                # Node.js sends invalid utf8 even though we're calling write(string, "utf8")
                # Python 2 can figure it out, but python 3 hates it and will die here with some byte sequences
                # Instead of crashing the plugin, we drop the data. Yes, this is horrible.
                before = before.decode('utf-8', 'ignore')
                data = json.loads(before)
            except Exception as e:
                msg.error('Unable to parse json: %s' % str(e))
                msg.error('Data: %s' % before)
                # XXXX: THIS LOSES DATA
                self._buf = after
                continue
            name = data.get('name')
            try:
                self.emit("data", name, data)
                msg.debug("got data " + name)
            except Exception as e:
                print(e)
                msg.error('Error handling %s event (%s).' % (name, str(e)))
                if name == 'room_info':
                    editor.error_message('Error joining workspace: %s' % str(e))
                    self.stop()
            self._buf = after

    def listen(self, listener):
        self._q.clear()
        reactor.reactor.select()

    def _connect(self, attempts=0):
        if attempts > 500:
            msg.error('Connection attempt timed out.')
            return self._reconnect()
        if not self._sock:
            msg.debug('_connect: No socket')
            return
        try:
            self._sock.connect((self.host, self.port))
            select.select([self._sock], [self._sock], [], 0)
        except socket.error as e:
            if e.errno == iscon_errno:
                pass
            elif e.errno in connect_errno:
                return utils.set_timeout(self._connect, 20, attempts + 1)
            else:
                msg.error('Error connecting:', e)
                return self._reconnect()
        if self.secure:
            sock_debug('SSL-wrapping socket')
            self._sock = ssl.wrap_socket(self._sock, ca_certs=self._cert_path, cert_reqs=ssl.CERT_REQUIRED, do_handshake_on_connect=False)

        self._q.clear()
        self.reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self.retries = self.MAX_RETRIES
        self.emit("connect")
        self.connected = True
        reactor.reactor.select()

    def __len__(self):
        return len(self._q)

    def fileno(self):
        return self._sock.fileno()

    def fd_set(self, readable, writeable, errorable):
        if not self.connected and not self._listener:
            return

        fileno = self.fileno()
        errorable.append(fileno)

        if self._listener:
            readable.append(fileno)

        if self._needs_handshake:
            return writeable.append(fileno)
        elif len(self) > 0:
            writeable.append(fileno)

        readable.append(fileno)

    def connect(self):
        utils.cancel_timeout(self._reconnect_timeout)
        self._reconnect_timeout = None
        self.cleanup()

        self._empty_selects = 0

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setblocking(False)
        if self.secure:
            if ssl:
                with open(self._cert_path, 'wb') as cert_fd:
                    cert_fd.write(cert.CA_CERT.encode('utf-8'))
            else:
                msg.log('No SSL module found. Connection will not be encrypted.')
                self.secure = False
                if self.port == G.DEFAULT_PORT:
                    self.port = 3148  # plaintext port
        conn_msg = 'Connecting to %s:%s' % (self.host, self.port)
        msg.log(conn_msg)
        editor.status_message(conn_msg)
        self._connect()

    def cleanup(self, *args, **kwargs):
        try:
            self._sock.shutdown(2)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        G.JOINED_WORKSPACE = False
        self._buf = bytes()
        self._sock = None
        self.connected = False

    def write(self):
        sock_debug('Socket is writeable')
        if self._needs_handshake:
            # sock_debug('Socket is writeable')
            try:
                sock_debug('Doing SSL handshake')
                self._sock.do_handshake()
            except ssl.SSLError as e:
                sock_debug('ssl.SSLError. This is expected sometimes.')
                return
            except Exception as e:
                msg.error('Error in SSL handshake:', e)
                self.reconnect()
                return

            self._needs_handshake = False
            sock_debug('Successful handshake')
            return

        try:
            while True:
                # TODO: use sock.send()
                item = self._q.popleft()
                sock_debug('sending patch', item)
                self._sock.sendall(item.encode('utf-8'))
        except IndexError:
            sock_debug('Done writing for now')

    def read(self):
        if self._listen:
            self._sock.accept()

        sock_debug('Socket is readable')
        buf = ''.encode('utf-8')
        while True:
            try:
                d = self._sock.recv(65536)
                if not d:
                    break
                buf += d
            except (AttributeError):
                return self.reconnect()
            except (socket.error, TypeError):
                break

        if buf:
            self._empty_reads = 0
            # sock_debug('read data')
            return self._handle(buf)

        # sock_debug('empty select')
        self._empty_reads += 1
        if self._empty_reads > (2000 / G.TICK_TIME):
            msg.error('No data from sock.recv() {0} times.'.format(self._empty_reads))
            return self.reconnect()

    def error(self):
        raise NotImplemented()

    def stop(self):
        self.retries = -1
        utils.cancel_timeout(self._reconnect_timeout)
        self._reconnect_timeout = None
        self.cleanup()
        msg.log('Disconnected.')

    def reconnect(self):
        if self._reconnect_timeout:
            return
        self.cleanup()
        self._reconnect_delay = min(10000, int(1.5 * self._reconnect_delay))

        if self._retries > 0:
            msg.log('Floobits: Reconnecting in %sms' % self._reconnect_delay)
            self._reconnect_timeout = utils.set_timeout(self.connect, self._reconnect_delay)
        elif self._retries == 0:
            editor.error_message('Floobits Error! Too many reconnect failures. Giving up.')
        self._retries -= 1

    def put(self, item):
        if not item:
            return
        msg.debug('writing %s: %s' % (item.get('name', 'NO NAME'), item))
        self._q.append(json.dumps(item) + '\n')
        qsize = len(self._q)
        msg.debug('%s items in q' % qsize)
        return qsize
