from __future__ import print_function, unicode_literals
from attr import attrs, attrib
import os, sys, struct, hashlib
import six
from tqdm import tqdm
from twisted.python import usage, failure
from twisted.python.filepath import FilePath
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.task import react
from twisted.internet.protocol import Protocol, ClientFactory, ServerFactory
from twisted.internet.interfaces import IPullProducer
from twisted.protocols import basic
from wormhole import create, input_with_completion
from wormhole.util import bytes_to_dict, dict_to_bytes
from wormhole.observer import OneShotObserver
from wormhole.eventual import EventualQueue
from zope.interface import directlyProvides
from twisted.python import log; import sys; log.startLogging(sys.stderr)


relay_url = "ws://localhost:4000/v1"
APPID = "lothar.com/wormhole/dilated-file-xfer"

class Options(usage.Options):
    def parseArgs(self, mode, *args):
        if mode not in ["tx", "rx"]:
            raise usage.UsageError("mode must be 'tx' or 'rx', not '%s'" % mode)
        self.mode = mode
        self.args = args

assert len(struct.pack("<L", 0)) == 4
assert len(struct.pack("<Q", 0)) == 8

def to_be8(value):
    if not 0 <= value < 2**64:
        raise ValueError
    return struct.pack(">Q", value)

def from_be8(b):
    if not isinstance(b, bytes):
        raise TypeError(repr(b))
    if len(b) != 8:
        raise ValueError
    return struct.unpack(">Q", b)[0]

class ReceiveProtocol(Protocol):
    def __init__(self):
        super(Protocol, self).__init__()
        self._in_header = True
        self._header_buffer = b""
        self._receiver = None

    def connectionMade(self):
        # we could write a record here, for negotiation, but really that
        # should happen before this point
        pass

    def dataReceived(self, data):
        if self._in_header:
            self._header_buffer += data
            have = len(self._header_buffer)
            if have < 8:
                return
            header_size = from_be8(self._header_buffer[:8])
            if have < 8+header_size:
                return
            header_bytes = self._header_buffer[8:8+header_size]
            header = bytes_to_dict(header_bytes)
            self._receiver = self.factory.incoming_file(header)
            self._in_header = False
            data = self._header_buffer[8+header_size:]
            del self._header_buffer
            # fall through with remaining data
        resp_msg = self._receiver.write(data)
        if resp_msg:
            resp_bytes = dict_to_bytes(resp_msg)
            self.transport.write(to_be8(len(resp_bytes)))
            self.transport.write(resp_bytes)

    def connectionLost(self, why=None):
        if self._receiver:
            self._receiver.close()

@attrs
class Receiver:
    fd = attrib()
    remaining = attrib()
    hasher = attrib()
    progress = attrib()

    def write(self, data):
        print("Receiver.write")
        self.hasher.update(data)
        self.fd.write(data)
        self.progress.update(len(data))
        self.remaining -= len(data)
        if self.remaining == 0:
            return self.finish()
        elif self.remaining < 0:
            raise RuntimeError("Receiver overflow")

    def finish(self):
        print("Receiver.finish")
        self.fd.close()
        datahash_hex = self.hasher.hexdigest()
        ack = {"ack": "ok", "sha256": datahash_hex}
        return ack

    def close(self):
        if self.remaining != 0:
            raise RuntimeError("unexpected close")

class ReceiverFactory(ServerFactory):
    protocol = ReceiveProtocol

    def __init__(self):
        super(ServerFactory, self).__init__()
        self.downloads = FilePath(os.path.expanduser("~/Downloads"))

    def incoming_file(self, header):
        print("incoming_file", header)
        if header["type"] != "file":
            raise RuntimeError("unknown header.type '%s'" % header["type"])
        if "compression" in header:
            raise RuntimeError("unknown compression '%s'" % header["compression"])
        size = header["size"]
        if not isinstance(size, six.integer_types):
            raise RuntimeError("unknown size '%s' (%s)" % (size, type(size)))
        # TODO: header["type"]: file, tarball
        #  sending a whole directory should send us a tarball
        #  sending individual files should get us a file
        # TODO: header["compression"]
        target = self.downloads.child(header["name"])
        count = 1
        while target.exists():
            name = "%s (%d)" % (header["name"], count)
            count += 1
            target = self.downloads.child(name)
        fd = target.open("wb")
        # TODO: write to hidden tmpfile, then rename when complete
        print("receiving to %s" % target)
        hasher = hashlib.sha256()
        progress = tqdm(file=sys.stderr, disable=False,
                        unit="B", unit_scale=True,
                        total=size)
        return Receiver(fd, size, hasher, progress)


class SendProtocol(Protocol):
    def __init__(self, eq, fd, header, size):
        super(Protocol, self).__init__()
        self._fd = fd
        self._header = header
        self._size = size
        self._done_observer = OneShotObserver(eq)
        self._expected_hexhash = None
        self._ack_buffer = b""
        self._done = False

    def when_done(self):
        return self._done_observer.when_fired()

    def connectionMade(self):
        print("SendProtocol.connectionMade")
        header_bytes = dict_to_bytes(self._header)
        self.transport.write(header_bytes)
        hasher = hashlib.sha256()
        progress = tqdm(file=sys.stderr, disable=False,
                        unit="B", unit_scale=True, total=self._size)
        def _count_and_hash(data):
            hasher.update(data)
            progress.update(len(data))
            return data
        fs = basic.FileSender()
        # FileSender is marked as IProducer but not IPullProducer, which is
        # odd because it submits itself as one. Our code
        # (outbound.PullToPush) checks for IPullProducer and refuses to work
        # unless it is provided by the producer. So manually mark it here.
        directlyProvides(fs, IPullProducer)
        d = fs.beginFileTransfer(self._fd, self.transport, transform=_count_and_hash)
        def done(_):
            self._expected_hexhash = hasher.hexdigest()
        d.addCallback(done)
        d.addErrback(self._done_observer.fire)

    def dataReceived(self, data):
        print("SendProtocol.dataReceived")
        if self._done:
            raise RuntimeError("data after done")
        self._ack_buffer += data
        have = len(self._ack_buffer)
        if have < 8:
            return
        ack_size = from_be8(self._ack_buffer[:8])
        if have < 8+ack_size:
            return
        ack_bytes = self._ack_buffer[8:8+ack_size]
        ack = bytes_to_dict(ack_bytes)
        self._done = True
        self.transport.loseConnection()
        self.process_ack(ack)

    def process_ack(self, ack):
        print("SendProtocol.process_ack")
        if self._expected_hexhash is None:
            raise RuntimeError("premature ack")
        if ack["ack"] != "ok":
            raise RuntimeError("ack not ok: %s" % (ack,))
        if ack["sha256"] != self._expected_hexhash:
            raise RuntimeError("ack bad hash: got %s, expected %s" % (ack["sha256"], self._expected_hexhash))
        self._done_observer.fire("ok")

    def connectionLost(self, why=None):
        print("SendProtocol.connectionLost")
        if not self._done:
            f = failure.Failure("premature close")
            self._done_observer.error(f)

@attrs
class InstanceFactory(ClientFactory):
    instance = attrib()
    def buildProtocol(self, addr):
        return self.instance

class ControlProtocol(Protocol):
    def __init__(self, eq):
        super(Protocol, self).__init__()
        self._buffer = b""
        self._done = OneShotObserver(eq)

    def get_msg(self):
        return self._done.when_fired()

    def send(self, msg):
        msg_bytes = dict_to_bytes(msg)
        self.transport.write(to_be8(len(msg_bytes)))
        self.transport.write(msg_bytes)

    def dataReceived(self, data):
        self._buffer += data
        have = len(self._buffer)
        if have < 8:
            return
        msg_size = from_be8(self._buffer[:8])
        if have < 8+msg_size:
            return
        msg_bytes = self._buffer[8:8+msg_size]
        msg = bytes_to_dict(msg_bytes)
        self._done.fire(msg)

@inlineCallbacks
def open(reactor, options):
    eq = EventualQueue(reactor)
    print("creating")
    w = create(APPID, relay_url=relay_url, reactor=reactor,
               _enable_dilate=True)
    #if options.mode == "tx":
    #    w.allocate_code(2)
    #    code = yield w.get_code()
    #    print("Wormhole code is: %s" % code)
    #else:
    #    prompt = "Enter receive wormhole code: "
    #    yield input_with_completion(prompt, w.input_code(), reactor)
    #    #code = yield w.get_code()
    w.set_code("0")
    print("connecting..")
    (control_ep, client_ep, server_ep) = yield w.dilate()
    print("connected")

    cp = ControlProtocol(eq)
    control_ep.connect(InstanceFactory(cp))

    rf = ReceiverFactory()
    server_ep.listen(rf)

    if options.mode == "tx":
        for fn in options.args:
            fp = FilePath(fn)
            print("sending %s" % fp.basename())
            assert fp.isfile()
            header = {
                "type": "file",
                "size": fp.getsize(),
                "name": fp.basename(),
                }
            sp = SendProtocol(eq, fp.open("rb"), header, fp.getsize())
            client_ep.connect(InstanceFactory(sp))
            # we send one file at a time
            yield sp.when_done()
            print(" send done")
        cp.send({"done": "done"})
    else:
        print("waiting for files")
        yield cp.get_msg()

    # I'm expecting w.close() to close all subchannels and wait for their
    # outbound messages to be delivered before terminating the program
    yield w.close()
    returnValue(0)


def run():
    options = Options()
    options.parseOptions()
    print("calling react")
    return react(open, (options,))

run()
