# utils.py
import httpx
import config
import logging
from httpx_socks import AsyncProxyTransport

log = logging.getLogger(__name__)

def make_async_client() -> httpx.AsyncClient:
    """
    Creates a pre-configured httpx.AsyncClient.
    If NORD_USER and NORD_PASS are set, it configures the client to use the
    NordVPN SOCKS5 proxy via a special transport.
    """
    transport = None
    if config.NORD_USER and config.NORD_PASS:
        host = "amsterdam.nl.socks.nordhold.net"
        port = 1080
        proxy_url = f"socks5://{config.NORD_USER}:{config.NORD_PASS}@{host}:{port}"
        transport = AsyncProxyTransport.from_url(proxy_url)
        log.info(f"HTTP client configured to use SOCKS5 proxy transport at {host}:{port}")
    
    return httpx.AsyncClient(
        transport=transport,
        timeout=config.HTTP_TIMEOUT, 
        follow_redirects=True,
        http2=False  # http2 must be False when using a proxy transport
    )
