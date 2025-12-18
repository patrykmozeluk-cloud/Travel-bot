# app.py - Main Flask Application
import logging
import asyncio
from flask import Flask, request, jsonify
from datetime import datetime, timezone

# Local module imports
import config
from gcs_state import load_state, save_state_atomic, sanitizing_startup_check, prune_sent_links, remember_for_deletion, perform_delete_sweep
from feed_parser import process_all_sources
from ai_processing import analyze_batch, run_full_perplexity_audit # Updated import
from publishing import publish_digest_async, send_telegram_message_async
from utils import make_async_client 

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(file_handler)

log = logging.getLogger(__name__)

# ---------- APP INITIALIZATION ----------
app = Flask(__name__)

# ---------- CORE APPLICATION LOGIC ----------

async def process_and_publish_offers(state: dict, generation: int) -> bool:
    """
    Main orchestration function.
    Accepts the state and generation, modifies the state, and returns True if modified.
    """
    log.info("Starting a full processing run...")
    state_modified = False

    # 1. Fetch and Prepare Candidates
    detailed_candidates = await process_all_sources()
    
    if not detailed_candidates:
        log.info("No new candidates to process. Pruning old links.")
        pruned_count = prune_sent_links(state)
        return pruned_count > 0

    # 2. AI Analysis
    log.info(f"Proceeding with AI analysis for {len(detailed_candidates)} candidates.")
    
    candidate_chunks = [detailed_candidates[i:i + config.AI_BATCH_SIZE] for i in range(0, len(detailed_candidates), config.AI_BATCH_SIZE)]
    all_ai_results = []
    for i, chunk in enumerate(candidate_chunks):
        results = await analyze_batch(chunk)
        all_ai_results.extend(results)
        if i < len(candidate_chunks) - 1:
            wait_time = config.AI_BATCH_WAIT_SECONDS
            log.info(f"Processed chunk {i+1}/{len(candidate_chunks)}. Waiting {wait_time}s.")
            await asyncio.sleep(wait_time)

    if not all_ai_results:
        log.warning("AI analysis returned no results for any batch. Pruning and finishing.")
        pruned_count = prune_sent_links(state)
        return pruned_count > 0

    # 3. Process AI Results and Distribute Content (Audit-Then-Route Logic)
    candidates_by_id = {c['id']: c for c in detailed_candidates}
    now_utc = datetime.now(timezone.utc)
    now_utc_iso = now_utc.isoformat()
    
    # --- New Audit-Then-Route Logic ---

    # 1. Identify all high-value candidates for Perplexity audit
    perplexity_candidates = [
        r for r in all_ai_results
        if r.get('score') and int(r.get('score', 0)) >= 9
        and int(r.get('conviction', 10)) >= 7
    ]
    log.info(f"Found {len(perplexity_candidates)} candidates (Score >= 9 & Conviction >= 7) requiring Perplexity audit.")

    # 2. Audit all candidates and collect GEMs and FAIRs
    gem_offers = []
    fair_offers = []

    for candidate in perplexity_candidates:
        original_candidate = candidates_by_id.get(candidate.get("id"))
        if not original_candidate:
            log.warning(f"Orphaned AI result with ID {candidate.get('id')}. Skipping.")
            continue
        
        state["sent_links"][original_candidate['dedup_key']] = now_utc_iso
        state_modified = True
        
        log.info(f"Running Perplexity audit for '{candidate.get('title')}' (Score: {candidate.get('score')}).")
        offer_price = original_candidate.get('price') or candidate.get('price', 'Brak ceny')
        audit_result = await run_full_perplexity_audit(
            title=candidate.get('title'),
            price=offer_price,
            link=original_candidate['link']
        )
        
        full_offer_details = {**original_candidate, **candidate, **audit_result, 'ai_score': candidate.get('score')}

        if full_offer_details.get("verdict") == "GEM":
            gem_offers.append(full_offer_details)
        elif full_offer_details.get("verdict") == "FAIR":
            fair_offers.append(full_offer_details)
        else:
            log.warning(f"Perplexity audit REJECTED offer '{candidate.get('title')}'. Verdict: '{full_offer_details.get('verdict')}'. Discarding.")

    # 3. Decide on the 'Sztos Alert' from the collected GEMs
    today_str = now_utc.date().isoformat()
    if state.get('last_sztos_alert_date') != today_str:
        log.info("New day detected. Resetting daily 'Sztos Alert' time slots.")
        state['sztos_slots_used_today'] = []
        state['last_sztos_alert_date'] = today_str
        state_modified = True

    current_hour = now_utc.hour
    time_slot = "morning" if 0 <= current_hour < 12 else "afternoon" if 12 <= current_hour < 18 else "evening"
    slot_is_available = time_slot not in state.get('sztos_slots_used_today', [])

    offers_for_digest = fair_offers
    
    # Separate GEMs by origin continent
    european_gems = [offer for offer in gem_offers if offer.get('origin_continent') == 'Europa']
    non_european_gems = [offer for offer in gem_offers if offer.get('origin_continent') != 'Europa']
    
    sztos_published = False
    if european_gems and slot_is_available:
        # Sort European GEMs by the new sztos_score from Perplexity
        european_gems.sort(key=lambda x: int(x.get('sztos_score', 0)), reverse=True)
        
        best_european_gem = european_gems[0] # The highest-scored European GEM
        
        log.info(f"POST-AUDIT: Best European GEM offer '{best_european_gem.get('title')}' (Sztos Score: {best_european_gem.get('sztos_score')}) is a 'SZTOS ALERT' for the '{time_slot}' slot. Publishing immediately.")
        message = f"🔥 **SZTOS ALERT!** 🔥\n\n{best_european_gem.get('telegram_message', best_european_gem.get('title'))}"
        
        if config.TELEGRAM_CHANNEL_ID:
            message_id = await send_telegram_message_async(message_content=message, link=best_european_gem['link'], chat_id=config.TELEGRAM_CHANNEL_ID)
            if message_id:
                remember_for_deletion(state, config.TELEGRAM_CHANNEL_ID, message_id, best_european_gem['source_url'])
                state.setdefault('sztos_slots_used_today', []).append(time_slot)
                state_modified = True
                sztos_published = True
                log.info(f"Sztos Alert slot '{time_slot}' used. Used slots today: {state['sztos_slots_used_today']}")
        
        # The rest of the European GEMs go to the digest
        offers_for_digest.extend(european_gems[1:])
    elif not european_gems:
         log.info("No European GEM offers found in this batch. All GEMs will be routed to digest.")
    elif not slot_is_available:
        log.info(f"A 'Sztos Alert' for the '{time_slot}' slot has already been published today. Routing all European GEMs to digest.")
        offers_for_digest.extend(european_gems)

    # All non-European GEMs always go to the digest
    offers_for_digest.extend(non_european_gems)

    # 4. Route all other offers to the digest queue
    if offers_for_digest:
        log.info(f"Routing {len(offers_for_digest)} offers to the digest queue.")
        target_queue_name = 'evening_digest_queue' if 10 <= now_utc.hour < 20 else 'morning_digest_queue'
        
        for offer in offers_for_digest:
            existing_keys = {c.get('dedup_key') for c in state.get(target_queue_name, [])}
            if offer['dedup_key'] not in existing_keys:
                state.setdefault(target_queue_name, []).append(offer)
                state_modified = True
            else:
                log.info(f"Offer '{offer.get('title')}' already in '{target_queue_name}'. Skipping add.")

    # Mark all other (low-score) offers as seen
    low_score_offers = [
        r for r in all_ai_results 
        if r.get('category') == 'IGNORE' or (r.get('score') and int(r.get('score', 0)) < 9)
    ]
    for offer in low_score_offers:
        original_candidate = candidates_by_id.get(offer.get("id"))
        if original_candidate and original_candidate['dedup_key'] not in state["sent_links"]:
           state["sent_links"][original_candidate['dedup_key']] = now_utc_iso
           state_modified = True
           log.info(f"Marking low-score offer '{original_candidate.get('title')[:30]}...' as seen.")

    log.info("Processing complete.")
    pruned_count = prune_sent_links(state)
    return state_modified or (pruned_count > 0)


async def master_scheduler():
    """Coordinates the main tasks based on a schedule."""
    now_utc = datetime.now(timezone.utc)
    log.info(f"Master scheduler running at {now_utc.isoformat()}")

    # --- STATE INITIALIZATION ---
    # Load state once at the beginning of the run.
    state, generation = load_state()
    state_was_modified = False

    # Perform initial sanitizing check. If it fixes something, save immediately.
    fixed_count = sanitizing_startup_check(state)
    if fixed_count > 0:
        log.warning(f"CRITICAL REPAIR: Found and fixed {fixed_count} corrupted entries in state file.")
        try:
            save_state_atomic(state, generation)
            log.info("Successfully saved repaired state. Reloading to ensure consistency.")
            state, generation = load_state() # Reload after critical repair
        except Exception as e:
            log.critical(f"CRITICAL FAILURE: Could not save repaired state file. Aborting run. Error: {e}")
            return "Critical: State repair failed during save."

    # --- CORE LOGIC ---
    # Run ingestion and processing. This function will now modify the state object directly.
    log.info("Scheduler: Kicking off ingestion and processing.")
    processed_new = await process_and_publish_offers(state, generation)
    state_was_modified = state_was_modified or processed_new

    # Perform delete sweep. This also modifies the state object.
    deleted_count = await perform_delete_sweep(state)
    if deleted_count > 0:
        state_was_modified = True
        log.info(f"Scheduler: Performed delete sweep, {deleted_count} messages processed.")
    else:
        log.info("Scheduler: Delete sweep found no messages to process.")

    # Digest publication twice a day.
    if now_utc.hour == 10: # Morning digest publication hour
        log.info(f"Scheduler: It's a digest hour ({now_utc.hour}:00 UTC). Publishing morning digest.")
        await publish_digest_async(state, generation, 'morning_digest_queue')
        state_was_modified = True
    elif now_utc.hour == 20: # Evening digest publication hour
        log.info(f"Scheduler: It's a digest hour ({now_utc.hour}:00 UTC). Publishing evening digest.")
        await publish_digest_async(state, generation, 'evening_digest_queue')
        state_was_modified = True
    else:
        log.info("Scheduler: Not a digest hour. Skipping digest.")
    
    # --- FINAL STATE SAVE ---
    # Save the state once at the end if any of the above functions modified it.
    if state_was_modified:
        try:
            log.info("Changes were made during the run. Saving final state.")
            save_state_atomic(state, generation)
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED at end of master_scheduler: {e}")
    else:
        log.info("No changes to state were made during this run. Skipping final save.")

    log.info("Master scheduler run finished.")
    return "Scheduler run complete."


# ---------- FLASK ROUTES ----------
@app.route("/")
def index():
    return "Travel-Bot v7.0 (Modular) is running.", 200

@app.route("/run", methods=['POST'])
def run_main_scheduler():
    """Main endpoint to be triggered by a scheduler."""
    # We will implement proper async handling for Flask later if needed.
    # For now, we run the async loop to completion for each request.
    try:
        result = asyncio.run(master_scheduler())
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        log.exception("Error in /run (master_scheduler) endpoint")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    # Use Gunicorn in production, this is for local development
    port = int(config.PORT)
    app.run(host="0.0.0.0", port=port, debug=False)

@app.route("/publish-digest", methods=['POST', 'GET'])
def handle_manual_publish():
    auth_header = request.headers.get("X-Bot-Secret-Token")
    # Using TELEGRAM_SECRET for authentication
    if not config.TELEGRAM_SECRET or auth_header != config.TELEGRAM_SECRET:
        log.warning("Unauthorized attempt to manually publish digest.")
        return "Unauthorized", 401
    
    log.info("MANUAL TRIGGER: Force-publishing digest requested.")
    try:
        result = asyncio.run(publish_digest_async())
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        log.exception("Error in /publish-digest (manual publish) endpoint")
        return jsonify({"status": "error", "message": str(e)}), 500