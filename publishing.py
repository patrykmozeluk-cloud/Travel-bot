# publishing.py
import logging
import random
import html
import json
import asyncio # Added missing import
from typing import Dict, Any, List
from telegraph import Telegraph
from datetime import datetime # Keep datetime for strftime

import config
from utils import make_async_client
from gcs_state import load_state, save_state_atomic

_TRAVEL_IMAGES = list(set([
    "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1500835556837-99ac94a94552?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1533105079780-92b9be482077?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1504609773096-104ff2c73ba4?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1498503182468-3b51cbb6cb24?q=80&w=1000&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1530521954074-e64f6810b32d?q=80&w=1000&auto=format&fit=crop",
    # Original DIGEST_IMAGE_URLS from config.py
    "https://images.unsplash.com/photo-1516483638261-f4dbaf036963?q=80&w=2800&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D",
    "https://images.pexels.com/photos/3408744/pexels-photo-3408744.jpeg?auto=compress&cs=tinysrgb&w=1260&h=750&dpr=2",
    "https://cdn.pixabay.com/photo/2017/01/20/00/30/maldives-1993704_1280.jpg"
]))

log = logging.getLogger(__name__)


async def send_photo_with_button_async(chat_id: str, photo_url: str, caption: str, button_text: str, button_url: str) -> int | None:
    """Sends a photo with a caption and an inline button."""
    async with make_async_client() as client:
        try:
            # Tworzymy listę przycisków
            keyboard = [[{"text": button_text, "url": button_url}]]
            
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": keyboard
                }
            }
            url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendPhoto"
            
            r = await client.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Photo sent to {chat_id}: {photo_url}")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false for sendPhoto: {body}")
        except Exception as e:
            log.error(f"Telegram sendPhoto error to {chat_id} (URL: {photo_url}): {e}", exc_info=True)
    return None

async def send_telegram_message_async(message_content: str, link: str, chat_id: str) -> int | None:
    """Sends a standard offer message in Markdown with a button."""
    async with make_async_client() as client:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message_content,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[{"text": "👉 SPRAWDŹ OFERTĘ", "url": link}]]
                }
            }
            
            url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
            
            r = await client.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()

            if body.get("ok"):
                log.info(f"Message sent (Markdown): {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                log.error(f"Telegram returned ok=false: {body}")
                if body.get("description") and "can't parse entities" in body["description"]:
                    log.warning(f"MARKDOWN PARSE ERROR. Offending text was: \n---\n{message_content}\n---")

        except Exception as e:
            log.error(f"Telegram send error for {link}: {e}", exc_info=True)
    return None


async def publish_digest_async() -> str:
    """Generates and publishes the digest of top offers to Telegraph and posts a link to Telegram."""
    log.info("Starting digest generation...")
    state, generation = load_state()

    if not config.TELEGRAPH_TOKEN:
        log.error("TELEGRAPH_TOKEN is not configured. Cannot publish digest.")
        return "Error: Telegraph token not configured."

    digest_candidates = state.get("digest_candidates", [])
    
    # --- DIAGNOSTIC LOG ---
    log.info(f"DIAGNOSTIC: Starting publish_digest_async with candidates: {json.dumps(digest_candidates, indent=2, ensure_ascii=False)}")
    # ----------------------

    if not digest_candidates:
        log.info("Digest candidates list is empty. Skipping digest generation.")
        return "Digest candidates list is empty, no digest to generate."

    # Filter out malformed candidates from previous failed runs
    valid_candidates = [
        c for c in digest_candidates 
        if c.get('verdict') and c.get('analysis') and c.get('dedup_key')
    ]
    if len(valid_candidates) < len(digest_candidates):
        log.warning(f"Filtered out {len(digest_candidates) - len(valid_candidates)} malformed digest candidates.")
    
    if not valid_candidates:
        log.warning("No valid digest candidates found after filtering. Clearing list and skipping.")
        state["digest_candidates"] = []
        save_state_atomic(state, generation)
        return "No valid digest candidates to publish."

    # Remove duplicates, keeping the latest one
    unique_offers_dict = {offer['dedup_key']: offer for offer in valid_candidates}
    unique_offers = list(unique_offers_dict.values())
    log.info(f"Found {len(unique_offers)} unique, valid offers for the digest.")

    # Sort offers: 'GEM' first, then 'FAIR', then alphabetically
    def sort_key(offer):
        verdict_order = {"GEM": 1, "FAIR": 2} # Updated verdicts
        return (verdict_order.get(offer.get('verdict'), 99), offer.get('original_title', '').lower())

    sorted_offers = sorted(unique_offers, key=sort_key)
    
    super_deals = [o for o in sorted_offers if o.get('verdict') == "GEM"] # Updated verdict
    market_price_deals = [o for o in sorted_offers if o.get('verdict') == "FAIR"] # Updated verdict

    log.info(f"Digest breakdown: {len(super_deals)} GEM deals, {len(market_price_deals)} FAIR deals.")

    telegraph = Telegraph(config.TELEGRAPH_TOKEN)
    
    content_html = ""

    if super_deals:
        content_html += "<h3>💎 Super Okazje Dnia! 💎</h3>"
        content_html += "<p><i>Te oferty to prawdziwe perełki, które szybko znikają!</i></p>"
        for offer in super_deals:
            content_html += f"<h4>{html.escape(offer.get('hotel_name', offer.get('original_title', 'Brak tytułu')))}</h4>" # Use new hotel_name
            if offer.get('price_value'): content_html += f"<p><b>Cena:</b> {html.escape(str(offer['price_value']))} {html.escape(offer.get('currency', ''))}</p>" # Use new price fields
            if offer.get('telegram_message'): content_html += f"<p><b>Analiza:</b> {html.escape(offer['telegram_message'])}</p>" # Use new analysis field
            content_html += f"<p><b>Źródło:</b> {html.escape(offer.get('source_name', 'Nieznane'))}</p>"
            content_html += f"<p><a href='{offer['link']}'>👉 SPRAWDŹ OFERTĘ</a></p><hr/>"

    if market_price_deals:
        content_html += "<h3>✅ Pozostałe Zweryfikowane Oferty ✅</h3>" # Added emoji to both sides
        content_html += "<p><b>Dobre, solidne oferty, które warto rozważyć.</b></p><br/>"
        for offer in market_price_deals:
            content_html += f"<h4>{html.escape(offer.get('hotel_name', offer.get('original_title', 'Brak tytułu')))}</h4>" # Use new hotel_name
            if offer.get('price_value'): content_html += f"<p><b>Cena:</b> {html.escape(str(offer['price_value']))} {html.escape(offer.get('currency', ''))}</p>" # Use new price fields
            if offer.get('telegram_message'): content_html += f"<p><b>Analiza:</b> {html.escape(offer['telegram_message'])}</p>" # Use new analysis field
            content_html += f"<p><b>Źródło:</b> {html.escape(offer.get('source_name', 'Nieznane'))}</p>"
            content_html += f"<p><a href='{offer['link']}'>👉 SPRAWDŹ OFERTĘ</a></p><hr/>"
    
    # Fallback in case there are no deals to show
    if not content_html:
        log.warning("No deals to publish after filtering. Clearing candidates and skipping.")
        state["digest_candidates"] = []
        save_state_atomic(state, generation)
        return "No deals to publish."

    try:
        current_time = datetime.now()
        if 10 <= current_time.hour < 15:
            digest_title = f"Poranny Przegląd Ofert ({current_time.strftime('%d-%m-%Y')})"
        else:
            digest_title = f"Popołudniowy Przegląd Ofert ({current_time.strftime('%d-%m-%Y')})"

        response = telegraph.create_page(
            title=digest_title,
            html_content=content_html,
            author_name="Travel Bot",
        )
        page_url = response['url']
        log.info(f"Successfully created Telegra.ph page: {page_url}")

        engaging_caption = "🔥 <b>GORĄCA SELEKCJA OFERT CZEKA!</b> 🔥\n\nSprawdź nasze najnowsze, zweryfikowane okazje. Niektóre z nich to prawdziwe perełki!\n\n<i>Kliknij poniżej, aby zobaczyć pełny przegląd!</i>"
        digest_button_text = "👉 Zobacz Pełen Przegląd!"
        
        selected_photo_url = random.choice(_TRAVEL_IMAGES)

        if config.TELEGRAM_CHANNEL_ID:
            await send_photo_with_button_async(
                chat_id=config.TELEGRAM_CHANNEL_ID,
                photo_url=selected_photo_url,
                caption=engaging_caption,
                button_text=digest_button_text,
                button_url=page_url
            )

        state["digest_candidates"] = []
        save_state_atomic(state, generation)
        log.info("Digest published and candidates list has been cleared.")
        
        return f"Digest published successfully: {page_url}"

    except Exception as e:
        log.error(f"Failed to create or publish Telegra.ph page: {e}", exc_info=True)
        return "Error during digest publication."
