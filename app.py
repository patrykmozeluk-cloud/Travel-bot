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

    # 3. Process AI Results and Distribute Content ("Sztos vs Reszta")
    candidates_by_id = {c['id']: c for c in detailed_candidates}
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    
    for ai_result in all_ai_results:
        result_id = ai_result.get("id")
        if result_id is None:
            log.warning(f"AI result with no ID found: {ai_result}. Skipping.")
            continue

        original_candidate = candidates_by_id.get(result_id)
        if not original_candidate:
            log.warning(f"AI returned a result with ID {result_id} that does not match any original candidate. Skipping.")
            continue
            
        category = ai_result.get("category")
        score = ai_result.get("score")
        if not category:
            log.warning(f"AI result for {original_candidate['title']} has no category. Skipping.")
            continue
            
        # Save the deduplication key to the sent_links state with a simple timestamp.
        state["sent_links"][original_candidate['dedup_key']] = now_utc_iso
        state_modified = True

        if score == 10 and category == "PUSH":
            log.info(f"Offer '{ai_result.get('title', 'N/A')}' is a 'SZTOS' candidate (Score 10). Running Perplexity audit for verification.")
            
            offer_price = original_candidate.get('price') or ai_result.get('price', 'Brak ceny')
            audit_result = await run_full_perplexity_audit(
                title=ai_result.get('title'),
                price=offer_price,
                link=original_candidate['link']
            )
            verdict = audit_result.get("verdict")

            if verdict == "GEM":
                log.info(f"Perplexity audit VERIFIED Sztos offer with verdict 'GEM'. Sending immediately.")
                
                message = f"🔥 **SZTOS ALERT!** 🔥\n\n{audit_result.get('telegram_message', ai_result.get('title'))}"
                
                if config.TELEGRAM_CHANNEL_ID:
                    message_id = await send_telegram_message_async(
                        message_content=message,
                        link=original_candidate['link'],
                        chat_id=config.TELEGRAM_CHANNEL_ID
                    )
                    if message_id:
                        remember_for_deletion(state, config.TELEGRAM_CHANNEL_ID, message_id, original_candidate['source_url'])
                        state_modified = True
            
            elif verdict == "FAIR":
                log.info(f"Sztos offer was downgraded to 'FAIR' by Perplexity. Adding to digest instead of instant publish.")
                existing_keys = {c.get('dedup_key') for c in state.get("digest_candidates", [])}
                if original_candidate['dedup_key'] not in existing_keys:
                    candidate_to_add = {**original_candidate, **audit_result, 'ai_score': score}
                    state["digest_candidates"].append(candidate_to_add)
                    state_modified = True
                    log.info(f"Downgraded sztos '{ai_result.get('title', 'N/A')}' added to digest.")
                else:
                    log.info(f"Downgraded sztos '{ai_result.get('title', 'N/A')}' was already in digest. Skipping.")

            else:
                log.warning(f"Perplexity audit REJECTED Sztos offer. Verdict: '{verdict}'. Not sending or adding to digest.")

        elif score and score >= 7 and category == "DIGEST":
            log.info(f"Offer '{ai_result.get('title', 'N/A')}' is a 'DIGEST' candidate (Score: {score}). Running Perplexity audit.")
            offer_price = original_candidate.get('price') or ai_result.get('price', 'Brak ceny')
            
            audit_result = await run_full_perplexity_audit(
                title=ai_result.get('title'),
                price=offer_price,
                link=original_candidate['link']
            )
            verdict = audit_result.get("verdict")

            if verdict in ["GEM", "FAIR"]:
                log.info(f"Perplexity audit for '{ai_result.get('title', 'N/A')}' with verdict '{verdict}'. Adding to digest.")
                existing_keys = {c.get('dedup_key') for c in state.get("digest_candidates", [])}
                if original_candidate['dedup_key'] not in existing_keys:
                    candidate_to_add = {**original_candidate, **audit_result, 'ai_score': score}
                    state["digest_candidates"].append(candidate_to_add)
                    state_modified = True
                else:
                    log.info(f"Offer '{ai_result.get('title', 'N/A')}' already in digest candidates. Skipping add.")
            else:
                log.info(f"Perplexity audit for '{ai_result.get('title', 'N/A')}' returned '{verdict}'. Not adding to digest.")
        else:
            log.info(f"Offer '{original_candidate['title'][:40]}...' is '{category}' (Score: {score}). Skipping publication.")

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

    # Digest publication twice a day. This function loads its own state but that's okay
    # as it's a separate, self-contained action. We pass the state to it for consistency.
    if now_utc.hour in [10, 20]: # Digest publication hours
        log.info(f"Scheduler: It's a digest hour ({now_utc.hour}:00 UTC). Publishing digest.")
        # This function will save the state internally after clearing the digest list.
        await publish_digest_async(state, generation)
        state_was_modified = True # Assume digest publication modifies state
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