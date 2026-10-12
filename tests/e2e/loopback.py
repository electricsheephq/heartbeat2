"""Network boundary shared by pytest and the isolated gateway's sitecustomize."""
import ipaddress
import socket


def install(setattr_fn=setattr):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    sendto, getaddrinfo = socket.socket.sendto, socket.getaddrinfo

    def check(address, family=None):
        if family == socket.AF_UNIX:
            return
        host = address[0] if isinstance(address, tuple) else address
        if host == "localhost":
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            allowed = False
        if not allowed:
            raise PermissionError("E2E network boundary: non-loopback address refused")

    def guarded_connect(sock, address):
        check(address, sock.family)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(address, sock.family)
        return connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        check(args[-1], sock.family)
        return sendto(sock, data, *args)

    def guarded_getaddrinfo(host, *args, **kwargs):
        check(host)
        return getaddrinfo(host, *args, **kwargs)

    for name, fn in (("connect", guarded_connect), ("connect_ex", guarded_connect_ex),
                     ("sendto", guarded_sendto)):
        setattr_fn(socket.socket, name, fn)
    setattr_fn(socket, "getaddrinfo", guarded_getaddrinfo)
