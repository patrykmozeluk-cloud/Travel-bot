# publishing.py
import logging
import random
import html
import json
import asyncio
import re
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

def format_for_telegraph(text: str) -> str:
    """Converts telegram_message Markdown (**bold**) and newlines to Telegraph-compatible HTML."""
    if not text:
        return ""
    # Convert **bold** to <b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Convert newlines to <br>
    text = text.replace('\n', '<br>')
    return text


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
    """Sends a standard offer message, with a fallback from Markdown to plain text."""
    
    # Payload with Markdown
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
    
    async with make_async_client() as client:
        try:
            # --- First Attempt: Send with Markdown ---
            r = await client.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
            
            # Check for "can't parse entities" specifically, which returns a 400
            if r.status_code == 400 and "can't parse entities" in r.text:
                # This is a predictable error, raise a specific exception to trigger the fallback
                raise ValueError("Markdown parse error, requires fallback.")
            
            r.raise_for_status() # Raise for other HTTP errors (e.g., 500)
            
            body = r.json()
            if body.get("ok"):
                log.info(f"Message sent (Markdown): {message_content[:60]}…")
                return body.get("result", {}).get("message_id")
            else:
                # If ok=false but it wasn't a 400 parse error, log it and fail.
                log.error(f"Telegram returned ok=false (Markdown): {body}")
                return None

        except Exception as e:
            log.warning(f"Failed to send with Markdown ('{e}'). Retrying as plain text.")
            
            # --- Second Attempt: Send as Plain Text ---
            try:
                # Remove parse_mode for plain text
                payload.pop("parse_mode", None)
                
                r_fallback = await client.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
                r_fallback.raise_for_status()
                
                body_fallback = r_fallback.json()
                if body_fallback.get("ok"):
                    log.info(f"Message sent (Plain Text Fallback): {message_content[:60]}…")
                    return body_fallback.get("result", {}).get("message_id")
                else:
                    log.error(f"Telegram returned ok=false on fallback: {body_fallback}")
            except Exception as fallback_e:
                log.error(f"Telegram fallback send error for {link}: {fallback_e}", exc_info=True)
                
    return None


async def publish_digest_async(state: Dict[str, Any] | None = None, generation: int | None = None, queue_name: str | None = None) -> str:
    """
    Generates and publishes the digest of top offers to Telegraph and posts a link to Telegram.
    Processes a specific queue ('morning_digest_queue' or 'evening_digest_queue').
    """
    log.info(f"Starting digest generation for queue: '{queue_name}'...")
    
    if not queue_name:
        return "Error: No digest queue name provided for publication."

    # If state is not passed, load it. This supports manual runs.
    if state is None or generation is None:
        log.info("State not provided, loading from GCS for a standalone run.")
        state, generation = load_state()

    if not config.TELEGRAPH_TOKEN:
        log.error("TELEGRAPH_TOKEN is not configured. Cannot publish digest.")
        return "Error: Telegraph token not configured."

    digest_candidates = state.get(queue_name, [])

    if not digest_candidates:
        log.info(f"Digest queue '{queue_name}' is empty. Skipping digest generation.")
        return f"Digest queue '{queue_name}' is empty, no digest to generate."

    # Sort offers: ai_score highest first, then alphabetically
    def sort_key(offer):
        # Prioritize ai_score (10 > 9), then alphabetical by title
        return (-int(offer.get('ai_score', 0)), offer.get('original_title', '').lower())

    sorted_offers = sorted(digest_candidates, key=sort_key)
    
    diamond_deals = [o for o in sorted_offers if int(o.get('ai_score', 0)) == 10]
    good_deals = [o for o in sorted_offers if int(o.get('ai_score', 0)) == 9]

    log.info(f"Digest breakdown for '{queue_name}': {len(diamond_deals)} DIAMOND (AI Score 10) deals, {len(good_deals)} GOOD (AI Score 9) deals.")

    telegraph = Telegraph(config.TELEGRAPH_TOKEN)
    
    content_html = ""

    if diamond_deals:
        content_html += "<h3>💎 Perełki Dnia (AI Score 10) 💎</h3>"
        content_html += "<p><i>To są absolutne HITY, zweryfikowane przez naszą AI i audyt Perplexity. Nie przegap!</i></p>"
        for offer in diamond_deals:
            # Używamy sformatowanej wiadomości z AI (Telegram Style)
            formatted_msg = format_for_telegraph(offer.get('telegram_message', "Kliknij, aby sprawdzić szczegóły tej wyjątkowej oferty!"))
            
            content_html += f"<p>{formatted_msg}</p>"
            content_html += f"<p><a href='{offer['link']}'>👉 SPRAWDŹ OFERTĘ</a></p><hr/>"

    if good_deals:
        content_html += "<h3>🌟 Dobre Okazje (AI Score 9) 🌟</h3>"
        content_html += "<p><b>Solidne oferty po audycie Perplexity, które zasługują na Twoją uwagę.</b></p><br/>"
        for offer in good_deals:
            # Używamy sformatowanej wiadomości z AI (Telegram Style)
            formatted_msg = format_for_telegraph(offer.get('telegram_message', "Kliknij, aby sprawdzić szczegóły tej dobrej okazji!"))

            content_html += f"<p>{formatted_msg}</p>"
            content_html += f"<p><a href='{offer['link']}'>👉 SPRAWDŹ OFERTĘ</a></p><hr/>"
            content_html += f"<p><b>Źródło:</b> {html.escape(offer.get('source_name', 'Nieznane'))}</p>"
            content_html += f"<p><a href='{offer['link']}'>👉 SPRAWDŹ OFERTĘ</a></p><hr/>"
    
    # Fallback in case there are no deals to show
    if not content_html:
        log.warning(f"No deals to publish from '{queue_name}' after filtering. Clearing queue and skipping.")
        state[queue_name] = []
        save_state_atomic(state, generation)
        return f"No deals to publish from '{queue_name}'."

    try:
        current_time = datetime.now()
        if queue_name == 'morning_digest_queue':
            digest_title = f"Poranny Przegląd Ofert ({current_time.strftime('%d-%m-%Y')})"
        else:
            digest_title = f"Popołudniowy Przegląd Ofert ({current_time.strftime('%d-%m-%Y')})"

        response = telegraph.create_page(
            title=digest_title,
            html_content=content_html,
            author_name="Travel Bot",
        )
        page_url = response['url']
        log.info(f"Successfully created Telegra.ph page for '{queue_name}': {page_url}")

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

        state[queue_name] = []
        save_state_atomic(state, generation)
        log.info(f"Digest for '{queue_name}' published and queue has been cleared.")
        
        return f"Digest for '{queue_name}' published successfully: {page_url}"

    except Exception as e:
        log.error(f"Failed to create or publish Telegra.ph page for '{queue_name}': {e}", exc_info=True)
        return "Error during digest publication."
