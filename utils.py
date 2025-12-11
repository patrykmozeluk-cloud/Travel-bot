# utils.py
import httpx
import config
import logging

log = logging.getLogger(__name__)

def make_async_client() -> httpx.AsyncClient:
    """
    Creates a pre-configured httpx.AsyncClient.
    If NORD_USER and NORD_PASS are set, it configures the client to use the
    NordVPN SOCKS5 proxy.
    """
    proxies = None
    if config.NORD_USER and config.NORD_PASS:
        host = "amsterdam.nl.socks.nordhold.net"
        port = 1080
        proxy_url = f"socks5://{config.NORD_USER}:{config.NORD_PASS}@{host}:{port}"
        proxies = {
            "all://": proxy_url,
        }
        log.info(f"HTTP client configured to use SOCKS5 proxy at {host}:{port}")
    
    return httpx.AsyncClient(
        proxies=proxies,
        timeout=config.HTTP_TIMEOUT, 
        follow_redirects=True, 
        http2=False
    )
