"""Loopback URL validation extracted from H3 Prompt Studio's MIT resources module."""
from urllib.parse import urlparse


def local_url(url, suffix=''):
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Choose a local HTTP address on this computer.')
    if not parsed.port:
        raise ValueError('Include the port of the local worker.')
    return url.rstrip('/') + suffix
