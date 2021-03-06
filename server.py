# -*- encoding: utf-8 -*-
#!/usr/bin/env python2

# This code is adapted from the NFCGate Server Code
# https://github.com/nfcgate/server
# The NFCGate Server is licensed under the Apache License v2
#
# The NFCGate server code code was in turn inspired by
# http://www.binarytides.com/code-chat-application-server-client-sockets-python

import select
import socket
import ssl
import struct
import sys
import zlib

from messages.c2s_pb2 import ServerHello, StoreReply, DeleteReply, GetReply
from messages.metaMessage_pb2 import Wrapper
from messages.studyMessage_pb2 import StudyCreate, StudyCreateReply, StudyDelete, StudyDeleteReply, StudyWrapper, StudyJoinQuery, StudyJoinQueryReply, StudyListQuery, StudyListReply
from storage.sqlite import SqliteBackend
from vicbf.vicbf import VICBF
from hashlib import sha256
from Crypto.PublicKey import RSA
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5


class Cache():
    def __init__(self):
        self.vicbfcache = None

    def getVicbfCache(self):
        if self.vicbfcache is not None:
            debug("Cache hit")
            return self.vicbfcache
        else:
            debug("Cache miss")
            serialized = VicbfBackend.serialize().tobytes()
            self.vicbfcache = zlib.compress(serialized, 6)
            return self.vicbfcache

    def invalidateVicbf(self):
        debug("Cache invalidated")
        self.vicbfcache = None


HOST = "0.0.0.0"
PORT = 5566

DEBUG = True

DatabaseBackend = None

VicbfBackend = None
VicbfCache = Cache()

THRESH_UP = None


### Logging helper functions
def debug(strng):
    """Print a debugging string if debugging is active.

    Displays the function name of the caller to make debugging easier.
    Caller detection code adapted from:
    http://jugad2.blogspot.in/2015/09/find-caller-and-callers-caller-of.html
    """
    if DEBUG:
        print sys._getframe(1).f_code.co_name + ":", strng


### Network helper functions
def SocketReadN(sock, n):
    buf = b''
    while n > 0:
        data = sock.recv(n)
        if data == b'':
            raise RuntimeError('unexpected connection close')
        buf += data
        n -= len(data)
    return buf


def RecvOneMsg(sock):
    # Messages are prefixed with a 4-byte length indicator
    lengthbuf = SocketReadN(sock, 4)
    length = struct.unpack('>i', lengthbuf)[0]
    wrapper = Wrapper()
    try:
        wrapper.ParseFromString(SocketReadN(sock, length))
    except Exception, e:
        debug("ERROR: Message parsing failed: " + e)
        wrapper = None
    return wrapper


def sendMessage(msg, sock):
    if msg is None:
        return
    ms = msg.SerializeToString()
    # mb = [elem.encode('hex') for elem in ms]
    # Messages are sent as byte strings prefixed with their own length
    sock.sendall(struct.pack(">i", len(ms)) + ms)
    debug("Message sent")


### Debugging helper functions
def prettyPrintProtobuf(msg, sock):
    pass  # TODO Reimplement


### Helper function for the VICBF
def getVicbfSerialization():
    return VicbfCache.getVicbfCache()


def invalidateVicbfSerializationCache():
    VicbfCache.invalidateVicbf()


### Format checker helper functions
def keyFormatValid(key):
    return len(key) == 32


def queueFormatValid(queue):
    return len(queue) == 16


### Crypto helper functions
def pubkeyFromBytes(pkcs):
    return RSA.importKey(pkcs)


def verifyPKCS15_SHA256(pub, data, sig):
    # Create hash object
    h = SHA256.new(data)
    # Create verifier
    verifier = PKCS1_v1_5.new(pub)
    # Return verification result
    return verifier.verify(h, sig)

##### Message Creation Functions
# Example function:
# def getSessionMessage(code_tuple):
#     imsg = c2s_pb2.Session()
#     imsg.opcode = code_tuple[0]
#     imsg.errcode = code_tuple[1]
#     msg = metaMessage_pb2.Wrapper()
#     msg.Session.MergeFrom(imsg)
#     return msg


##### Handlers
# Handler for ClientHello messages
def HandleClientHelloMessage(msg, sock):
    rv = ServerHello()
    rv.serverProto = "1.0"
    if msg.clientProto == "1.0":
        # We are talking protocol version 1.0
        debug("Valid clientProto received")
        # Set Opcode to indicate compatibility
        rv.opcode = ServerHello.CLIENT_HELLO_OK
        # Add serialized Bloom Filter
        rv.data = getVicbfSerialization()
    else:
        # We don't know the protocol version the other party is speaking
        debug("WARN: Invalid clientProto received")
        # Set opcode to indicate incompatibility
        rv.opcode = ServerHello.CLIENT_HELLO_PROTO_NOT_SUPPORTED
        # Set required data field to placeholder
        rv.data = b'0'
    # Pack reply into Wrapper message
    wrapper = Wrapper()
    wrapper.ServerHello.MergeFrom(rv)
    # Return reply
    return wrapper


# Handler for Store messages
def HandleStoreMessage(msg, sock):
    rv = StoreReply()
    rv.key = msg.key
    if keyFormatValid(msg.key):
        # The key is valid
        try:
            # Insert into database
            DatabaseBackend.insert_kv(msg.key, msg.value)
            debug("Inserted into DB")
            # Insert into VICBF
            VicbfBackend.insert(msg.key)
            debug("Inserted into VICBF")
            # Invalidate VICBF cache
            invalidateVicbfSerializationCache()
            # Set opcode to indicate success
            rv.opcode = StoreReply.STORE_OK
            debug("Done")
        except KeyError:
            # The KeyError was thrown by the Database backend and indicates
            # that the key is already taken
            debug("WARN: Attempt to insert key that is already taken")
            rv.opcode = StoreReply.STORE_FAIL_KEY_TAKEN
        except Exception, e:
            # An unexpected exception was thrown - this indicates a bug
            debug("ERROR: Unexpected exception: " + repr(e))
            rv.opcode = StoreReply.STORE_FAIL_UNKNOWN
    elif queueFormatValid(msg.key):
        # Key is not a regular message, but an encrypted StudyJoin
        try:
            # Insert into queue
            if DatabaseBackend.insert_studyjoin(msg.key, msg.value):
                # Insert into queue worked
                rv.opcode = StoreReply.STORE_OK
            else:
                rv.opcode = StoreReply.STORE_FAIL_KEY_FMT
                # Not strictly true, the key format is fine
                # TODO Fix?
        except Exception, e:
            # Unexpected exception was thrown - bug?
            debug("ERROR: Unexpected exception: " + repr(e))
            rv.opcode = StoreReply.STORE_FAIL_UNKNOWN
    else:
        # Key has an invalid format, ignore message
        debug("WARN: Invalid key format")
        rv.opcode = StoreReply.STORE_FAIL_KEY_FMT
    # Create wrapper message
    wrapper = Wrapper()
    # Merge StoreReply into it
    wrapper.StoreReply.MergeFrom(rv)
    # Return the reply
    return wrapper


def HandleDeleteMessage(msg, sock):
    # Prepare DeleteReply message
    rv = DeleteReply()
    # Set the key
    rv.key = msg.key
    # Check if the key format is okay
    if keyFormatValid(msg.key):
        debug("Key format valid")
        # Check if the key is on the server
        if msg.key in VicbfBackend:
            debug("Key in VICBF")
            # Check if the auth hashes to the key
            if sha256(msg.auth).digest() == msg.key:
                debug("Authenticator good")
                # Delete the KV pair
                DatabaseBackend.delete_kv(msg.key)
                debug("Deleted from DB backend")
                # Delete the key from the VICBF
                VicbfBackend.remove(msg.key)
                debug("Deleted from VICBF")
                # Invalidate VICBF cache
                invalidateVicbfSerializationCache()
                # Set opcode to success
                rv.opcode = DeleteReply.DELETE_OK
                debug("Deleted kv pair")
            else:
                debug("WARN: Bad authenticator")
                # Authentication string does not hash to key
                rv.opcode = DeleteReply.DELETE_FAIL_AUTH
        else:
            debug("WARN: Key not found")
            # Key has not been stored on the server
            rv.opcode = DeleteReply.DELETE_FAIL_NOT_FOUND
    else:
        debug("WARN: Bad key format")
        # Key is in bad format
        rv.opcode = DeleteReply.DELETE_FAIL_KEY_FMT
    # Create wrapper message
    wrapper = Wrapper()
    # Merge DeleteReply into it
    wrapper.DeleteReply.MergeFrom(rv)
    # Return reply
    return wrapper


def HandleGetMessage(msg, sock):
    # Prepare GetReply message
    rv = GetReply()
    # Set the key
    rv.key = msg.key
    # Check if the key is valid
    if keyFormatValid(msg.key):
        # Retrieve value from database, if available
        value = DatabaseBackend.query_kv(msg.key)
        # Check if we actually got a value
        if value is not None:
            debug("Got value")
            # We got a value! Save it to the GetReply message
            rv.value = str(value)
            # Set the opcode to indicate success
            rv.opcode = GetReply.GET_OK
        else:
            debug("WARN: Unknown key requested")
            # Seems like we have no associated value - set opcode to indicate
            # failure
            rv.opcode = GetReply.GET_FAIL_UNKNOWN_KEY
    else:
        debug("WARN: Malformed key")
        # Set the opcode to indicate that the key was malformed
        rv.opcode = GetReply.GET_FAIL_KEY_FMT
    # Prepare a wrapper
    wrapper = Wrapper()
    # Merge GetReply into it
    wrapper.GetReply.MergeFrom(rv)
    # Return reply
    return wrapper


def HandleStudyWrapperMessage(msg, sock):
    mtype = msg.type
    if mtype == StudyWrapper.MSG_STUDYCREATE:
        debug("Got StudyCreate message")
        return HandleStudyCreateMessage(msg, sock)
    elif mtype == StudyWrapper.MSG_STUDYJOINQUERY:
        debug("Got StudyJoinQuery message")
        return HandleStudyJoinQuery(msg, sock)
    elif mtype == StudyWrapper.MSG_STUDYDELETE:
        debug("Got StudyDelete message")
        return HandleStudyDeleteMessage(msg, sock)
    else:
        debug("Unknown message type received")
        return None


def HandleStudyCreateMessage(msg, sock):
    # Parse StudyCreate
    screate = StudyCreate()
    screate.ParseFromString(msg.message)
    # Prepare reply
    sreply = StudyCreateReply()
    sreply.queueIdentifier = screate.queueIdentifier
    wrapper = Wrapper()
    # Read out public key and verify signature
    pub = pubkeyFromBytes(screate.publicKey)
    if not verifyPKCS15_SHA256(pub, msg.message, msg.signature):
        debug("ERROR: Invalid signature on message")
        sreply.status = StudyCreateReply.CREATE_FAIL_SIGNATURE
        wrapper.StudyCreateReply.MergeFrom(sreply)
        return wrapper
    if not queueFormatValid(screate.queueIdentifier):
        debug("ERROR: Invalid queue identifier")
        sreply.status = StudyCreateReply.CREATE_FAIL_BAD_IDENTIFIER
        wrapper.StudyCreateReply.MergeFrom(sreply)
        return wrapper
    # Insert into database
    if DatabaseBackend.insert_study(screate.queueIdentifier, screate.publicKey,
                                    msg):
        # Prepare reply
        sreply.status = StudyCreateReply.CREATE_OK
    else:
        sreply.status = StudyCreateReply.CREATE_FAIL_IDENTIFIER_TAKEN
    wrapper.StudyCreateReply.MergeFrom(sreply)
    return wrapper


def HandleStudyListRequest(msg, sock):
    # The message itself is not interesting, as it does not contain any
    # information - it's just an empty request
    reply = StudyListReply()
    for msg in DatabaseBackend.list_studies():
        wrapper = reply.studylist.add()
        wrapper.ParseFromString(msg[0])
    replywrapper = Wrapper()
    replywrapper.StudyListReply.MergeFrom(reply)
    return replywrapper


def HandleStudyJoinQuery(msg, sock):
    # We received a StudyJoinQuery message
    # Prepare a StudyJoinQueryReply
    reply = StudyJoinQueryReply()
    # Parse StudyJoinQuery message from msg
    request = StudyJoinQuery()
    request.ParseFromString(msg.message)
    # Retrieve public key from database
    pkey_bin = DatabaseBackend.query_study_pkey(request.queueIdentifier)
    if pkey_bin is not None:
        # Found a public key
        pkey = pubkeyFromBytes(pkey_bin)
        if not verifyPKCS15_SHA256(pkey, msg.message, msg.signature):
            reply.status = StudyJoinQueryReply.STATUS_FAIL_SIGNATURE
        else:
            reply.status = StudyJoinQueryReply.STATUS_OK
            blocks = DatabaseBackend.query_study(request.queueIdentifier)
            for element in blocks:
                reply.message.append(str(element[0]))
    else:
        # No public key found => No such study
        reply.status = StudyJoinQueryReply.STATUS_FAIL_NOT_FOUND
    # Prepare and return reply wrapper
    wrapper = Wrapper()
    wrapper.StudyJoinQueryReply.MergeFrom(reply)
    return wrapper


def HandleStudyDeleteMessage(msg, sock):
    # We received a StudyDelete message
    # Prepare a StudyDeleteReply
    reply = StudyDeleteReply()
    # Parse StudyDelete
    request = StudyDelete()
    request.ParseFromString(msg.message)
    # Retrieve public key from database
    pkey_bin = DatabaseBackend.query_study_pkey(request.queueIdentifier)
    if pkey_bin is not None:
        # Found a public key
        pkey = pubkeyFromBytes(pkey_bin)
        if not verifyPKCS15_SHA256(pkey, msg.message, msg.signature):
            reply.status = StudyDeleteReply.DELETE_FAIL_BAD_SIG
        else:
            # Verification worked, delete Study
            DatabaseBackend.delete_study(request.queueIdentifier)
            reply.status = StudyDeleteReply.DELETE_OK
    else:
        reply.status = StudyDeleteReply.DELETE_FAIL_BAD_IDENT
    # Prepare, fill and return wrapper
    wrapper = Wrapper()
    wrapper.StudyDeleteReply.MergeFrom(reply)
    return wrapper


# Handler for all incoming messages
def HandleMessage(message, sock):
    mtype = message.WhichOneof('message')
    if mtype == "ClientHello":
        debug("Received ClientHello")
        return HandleClientHelloMessage(message.ClientHello, sock)
    elif mtype == "Store":
        debug("Received Store")
        return HandleStoreMessage(message.Store, sock)
    elif mtype == "Delete":
        debug("Received Delete")
        return HandleDeleteMessage(message.Delete, sock)
    elif mtype == "Get":
        debug("Received Get")
        return HandleGetMessage(message.Get, sock)
    elif mtype == "StudyWrapper":
        debug("Received StudyWrapper")
        return HandleStudyWrapperMessage(message.StudyWrapper, sock)
    elif mtype == "StudyListQuery":
        debug("Received StudyListQuery")
        return HandleStudyListRequest(message.StudyListQuery, sock)
    # and so on


##### Main code
if __name__ == "__main__":

    CONNECTION_LIST = []  # list of socket clients
    RECV_BUFFER = 4096    # Advisable to keep it as an exponent of 2

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    server_socket.bind((HOST, PORT))
    server_socket.listen(10)

    # Add server socket to the list of readable connections
    CONNECTION_LIST.append(server_socket)

    # Prepare the database
    print "Initialize database"
    DatabaseBackend = SqliteBackend()

    print "Read existing keys into VICBF"
    # Read existing keys from the database
    keys = DatabaseBackend.all_keys()
    # Calculate the number of expected entries
    # For now, we will expect the number of entries to double, and add 1000
    # to the estimation to account for very small initial values.
    # This can probably be heavily optimized
    expected_entries = len(keys) * 2 + 1000
    # Taking 10 times the number of expected entries for the slot count will
    # result in a FPR of p = ~0.0007, or 0.07% once the number of expected
    # entries is reached.
    slots = expected_entries * 10
    # Calculate the threshold at which we should generate a new VICBF.
    # After having inserted double the expected entries in the VICBF, the FPR
    # will be at roughly p = 0.006, or 0.6%. At this point, we should generate
    # a new, larger VICBF to accomodate further entries
    THRESH_UP = expected_entries * 2
    # Initialize the VICBF with the given values
    VicbfBackend = VICBF(slots, 3)
    # Insert all existing keys into the VICBF
    for key in DatabaseBackend.all_keys():
        VicbfBackend += str(key[0])
    # Since nothing time-critical is happening right now, we can take the time
    # to populate the VICBF serialization cache. It is guaranteed to be needed
    # at least once before becoming outdated, as it will be accessed on every
    # new connection. The following call will request the VICBF serialization,
    # which will be cached, and ignore the result.
    print "Populate cache"
    getVicbfSerialization()

    print "Denul server started on port " + str(PORT)

    try:
        while True:
            # Get the list sockets which are ready to be read through select
            read_sockets, write_sockets, error_sockets = \
                select.select(CONNECTION_LIST, [], [])

            for sock in read_sockets:

                # New connection
                if sock == server_socket:
                    # Handle the case in which there is a new connection
                    # recieved through server_socket
                    sockfd, addr = server_socket.accept()

                    # Wrap the socket in a SSL/TLS socket
                    socktls = ssl.wrap_socket(sockfd, server_side=True,
                                              certfile="server.crt",
                                              keyfile="server.key")
                    # I'd love to make this a more secure instance of an SSL
                    # socket, but sadly, this would require python 2.7.9+,
                    # which is not yet available in the ubuntu repos I am
                    # using.
                    # Right now, the socket still allows SSLv3 and RC4
                    # connections, which is horrible, but the alternative
                    # would be to only allow TLSv1 (and not v1.1 / v1.2),
                    # which would be bad form as well.
                    # Once a newer version of python is widely available, I may
                    # change the code to use an ssl.Context object with the
                    # correct settings for a secure socket.
                    CONNECTION_LIST.append(socktls)

                    print "Client (%s, %s) connected" % addr

                # Some incoming message from a client
                else:
                    # Data recieved from client, process it
                    try:
                        wrapperMsg = RecvOneMsg(sock)
                        if wrapperMsg:
                            reply = HandleMessage(wrapperMsg, sock)
                            # w_reply = wrapMessage(reply)
                            sendMessage(reply, sock)

                    # client disconnected, so remove from socket list
                    except Exception, e:
                        print "Client (%s, %s) is offline: %s" % (addr[0], addr[1], e)
                        sock.close()
                        CONNECTION_LIST.remove(sock)
                        continue

    # Catch KeyboardInterrupts to save state before exiting
    except KeyboardInterrupt:
        print "Interrupted. exiting"

    # Try to close all sockets, ignoring any errors
    for sock in CONNECTION_LIST:
        try:
            sock.close()
        except Exception:
            continue

    # If we reach this statement, the main loop has terminated
    # Close the socket
    server_socket.close()
