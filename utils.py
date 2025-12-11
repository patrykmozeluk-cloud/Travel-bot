# utils.py
import httpx
import config

def make_async_client() -> httpx.AsyncClient:
    """Creates a pre-configured httpx.AsyncClient."""
    return httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT, 
        follow_redirects=True, 
        http2=False
    )
