# gcs_state.py
import logging
import json
import time
import random
import re
import asyncio # New import for async operations
from typing import Dict, Any, Tuple
from datetime import datetime, timezone, timedelta # New imports for time calculations
from google.cloud import storage

import config
from utils import make_async_client # New import for httpx client

log = logging.getLogger(__name__)

# ---------- LAZY GCS CLIENT INITIALIZATION ----------
_storage_client = None
_blob = None

def _get_gcs_blob():
    """Initializes and returns the GCS blob object, creating it only on first use."""
    global _storage_client, _blob
    if _blob is None:
        if config.BUCKET_NAME and config.SENT_LINKS_FILE:
            log.info("Performing first-time initialization of GCS client.")
            _storage_client = storage.Client()
            _bucket = _storage_client.bucket(config.BUCKET_NAME)
            _blob = _bucket.blob(config.SENT_LINKS_FILE)
        else:
            log.warning("GCS BUCKET_NAME or SENT_LINKS_FILE not configured. State will not be persisted.")
            return None
    return _blob

# ---------- STATE MANAGEMENT FUNCTIONS ----------

def _default_state() -> Dict[str, Any]:
    """Returns the default structure for the application state."""
    return {
        "sent_links": {},
        "delete_queue": [],
        "last_ai_analysis_time": "1970-01-01T00:00:00Z",
        "digest_candidates": [],
    }

def _ensure_state_shapes(state: Dict[str, Any]):
    """
    Ensures the state object conforms to the default shape,
    pruning any obsolete keys that may exist in the loaded state.
    """
    default_keys = _default_state().keys()
    
    # Prune obsolete keys from the loaded state
    obsolete_keys = [key for key in state if key not in default_keys]
    if obsolete_keys:
        log.warning(f"Pruning obsolete keys from state: {obsolete_keys}")
        for key in obsolete_keys:
            del state[key]

    # Ensure all required keys exist, adding them if they don't
    for key, default_value in _default_state().items():
        state.setdefault(key, default_value)


def load_state() -> Tuple[Dict[str, Any], int | None]:
    """
    Loads the state from GCS. Returns a default state if the blob doesn't exist or on error.
    Also returns the blob's generation number for optimistic locking.
    """
    blob = _get_gcs_blob()
    if not blob:
        log.warning("GCS blob not available. Returning default state.")
        return (_default_state(), None)
    
    try:
        if not blob.exists():
            log.info("State file not found in GCS. Returning default state.")
            return _default_state(), None
        
        blob.reload()
        state_data = json.loads(blob.download_as_bytes())
        _ensure_state_shapes(state_data)
        return state_data, blob.generation
    except Exception as e:
        log.warning(f"Failed to load state from GCS, returning default state. Error: {e}")
        return _default_state(), None

def save_state_atomic(state: Dict[str, Any], gen: int | None):
    """
    Saves the state to GCS using a generation match to prevent race conditions.
    Retries on precondition failure.
    """
    blob = _get_gcs_blob()
    if not blob:
        log.error("Cannot save state, GCS blob not configured.")
        return

    payload = json.dumps(state).encode('utf-8')
    for _ in range(10): # Retry loop for optimistic locking
        try:
            blob.upload_from_string(payload, if_generation_match=gen, content_type="application/json")
            log.info(f"State successfully saved to GCS blob: {config.SENT_LINKS_FILE}")
            return
        except Exception as e:
            if "PreconditionFailed" in str(e) or "412" in str(e):
                log.warning("State save conflict (412 Precondition Failed). Reloading state and retrying.")
                time.sleep(random.uniform(0.3, 0.8))
                _, gen = load_state()
                continue
            log.error(f"An unexpected error occurred during state save: {e}", exc_info=True)
            raise
    
    raise RuntimeError("Atomic state save failed after multiple retries.")

def sanitizing_startup_check(state: Dict[str, Any]) -> int:
    """
    Checks and repairs the 'delete_queue' for corrupted chat_id entries.
    This is a one-off repair function to be run at startup.
    Returns the number of fixed entries.
    """
    if "delete_queue" not in state or not isinstance(state.get("delete_queue"), list):
        return 0

    fixed_entries_count = 0
    sanitized_queue = []
    
    id_pattern = re.compile(r"^(-?\d+)")

    for item in state.get("delete_queue", []):
        if not isinstance(item, dict) or "chat_id" not in item:
            sanitized_queue.append(item)
            continue

        chat_id = item["chat_id"]
        
        if isinstance(chat_id, str) and ' ' in chat_id:
            original_id = chat_id
            match = id_pattern.match(original_id)
            if match:
                clean_id = match.group(1)
                item["chat_id"] = clean_id
                fixed_entries_count += 1
                log.info(f"Sanitized chat_id: '{original_id}' -> '{clean_id}'")
            else:
                log.warning(f"Could not sanitize chat_id '{original_id}'. Keeping original but this is an error.")
        
        sanitized_queue.append(item)

    if fixed_entries_count > 0:
        state["delete_queue"] = sanitized_queue
        log.info(f"SANITIZING COMPLETE: Repaired {fixed_entries_count} entries in the delete_queue.")

    return fixed_entries_count

def remember_for_deletion(state: Dict[str, Any], chat_id: str, message_id: int, source_url: str):
    """
    Adds a message to the deletion queue in the state file.
    Note: This function is currently not called in the main application flow
    as the bot no longer publishes to the chat group. It's here for completeness.
    """
    log.info(f"remember_for_deletion called. DELETE_AFTER_HOURS: {config.DELETE_AFTER_HOURS}")
    if config.DELETE_AFTER_HOURS <= 0:
        return
        
    delete_at = (datetime.now(timezone.utc) + timedelta(hours=config.DELETE_AFTER_HOURS)).replace(minute=0, second=0, microsecond=0)
    state["delete_queue"].append({
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "delete_at": delete_at.isoformat(),
        "source_url": source_url
    })

async def perform_delete_sweep(state: Dict[str, Any]) -> int:
    """
    Processes the deletion queue, removing old messages from Telegram.
    """
    if not state.get("delete_queue"):
        return 0

    now = datetime.now(timezone.utc)
    
    keep_for_later, process_now = [], []
    for item in state["delete_queue"]:
        try:
            if datetime.fromisoformat(item["delete_at"]) > now:
                keep_for_later.append(item)
            else:
                process_now.append(item)
        except (ValueError, TypeError):
            log.warning(f"Skipping malformed item in delete_queue: {item}")
            continue

    if not process_now:
        return 0

    log.info(f"Performing delete sweep for {len(process_now)} items from the delete queue.")
    
    actually_deleted_count = 0
    cleaned_from_queue_count = 0
    final_queue = keep_for_later.copy()

    async with make_async_client() as client:
        tasks = []
        for item in process_now:
            url = f"https://api.telegram.org/bot{config.TG_TOKEN}/deleteMessage"
            tasks.append(client.post(url, json={"chat_id": item["chat_id"], "message_id": item["message_id"]}))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(results):
            item = process_now[i]
            item_id = item["message_id"]

            if isinstance(res, Exception) or res.status_code >= 500:
                final_queue.append(item) # Retry on server errors or network issues
                log.error(f"Error during message deletion for {item_id}, will retry. Error: {res}")
                continue

            if res.status_code == 200:
                actually_deleted_count += 1
                log.info(f"SUCCESS: Message {item_id} deleted successfully from Telegram.")
                continue

            # For 4xx errors (like 400 or 403), we assume the message is gone or un-deletable.
            # We log it and remove it from the queue, but don't count it as a successful deletion.
            cleaned_from_queue_count += 1
            try:
                error_desc = res.json().get('description', res.text)
            except json.JSONDecodeError:
                error_desc = res.text
            log.warning(f"Message {item_id} could not be deleted (status: {res.status_code}, reason: '{error_desc}'). Removing from queue.")

    total_processed = actually_deleted_count + cleaned_from_queue_count
    if total_processed > 0:
        state["delete_queue"] = final_queue
        log.info(f"--- Sweep Job Summary ---")
        log.info(f"Successfully deleted from Telegram: {actually_deleted_count}")
        log.info(f"Cleaned from queue (already gone or too old): {cleaned_from_queue_count}")
        log.info(f"Items to retry later: {len(process_now) - total_processed}")
        log.info(f"Final queue size: {len(final_queue)}")
        log.info(f"-------------------------")
    
    return total_processed

def prune_sent_links(state: Dict[str, Any]) -> int:
    """
    Removes old links from the state to prevent it from growing indefinitely.
    Returns the number of links that were pruned.
    """
    if config.DEDUP_TTL_HOURS <= 0:
        return 0
        
    prune_before = datetime.now(timezone.utc) - timedelta(hours=config.DEDUP_TTL_HOURS)
    original_count = len(state["sent_links"])
    
    links_to_keep = {}
    for key, value in state["sent_links"].items():
        timestamp_str = None
        
        # Gracefully handle both old string format and new dictionary format
        if isinstance(value, str):
            timestamp_str = value
        elif isinstance(value, dict) and 'timestamp' in value:
            timestamp_str = value['timestamp']

        # If we have a timestamp string, try to parse it
        if timestamp_str and isinstance(timestamp_str, str):
            try:
                # Check if the link is NOT stale
                if datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')) >= prune_before:
                    links_to_keep[key] = value
                # If it is stale, we just don't add it to links_to_keep
            except (ValueError, TypeError):
                # If timestamp is malformed, treat as stale and do not keep
                log.warning(f"Pruning malformed entry for key {key}. Value: {value}")
                pass
        else:
            # If value is not a string or a valid dict, prune it
            log.warning(f"Pruning entry with unexpected format for key {key}. Value: {value}")

    pruned_count = original_count - len(links_to_keep)

    if pruned_count > 0:
        log.info(f"Pruned {pruned_count} old links from state.")
        state["sent_links"] = links_to_keep
        
    return pruned_count