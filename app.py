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
from publishing import publish_digest_async
from utils import make_async_client 

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ---------- APP INITIALIZATION ----------
app = Flask(__name__)

# ---------- CORE APPLICATION LOGIC ----------

async def process_and_publish_offers():
    """
    Main orchestration function.
    """
    log.info("Starting a full processing run...")
    state, generation = load_state()

    # 1. Initial State Maintenance
    fixed_count = sanitizing_startup_check(state)
    if fixed_count > 0:
        log.warning(f"CRITICAL REPAIR: Found and fixed {fixed_count} corrupted entries in state file.")
        try:
            save_state_atomic(state, generation)
            log.info("Successfully saved repaired state. Reloading state to continue run.")
            state, generation = load_state()
        except Exception as e:
            log.critical(f"CRITICAL FAILURE: Could not save repaired state file. Aborting run. Error: {e}")
            return "Critical: State repair failed during save."

    # 2. Fetch and Prepare Candidates
    detailed_candidates = await process_all_sources()
    
    if not detailed_candidates:
        log.info("No new candidates to process. Pruning old links and finishing run.")
        prune_sent_links(state)
        try: 
            save_state_atomic(state, generation)
        except Exception as e:
            log.critical(f"FINAL STATE SAVE FAILED after pruning: {e}")
        return "Run complete. No new posts."

    # 3. AI Analysis
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
        prune_sent_links(state)
        save_state_atomic(state, generation)
        return "Run complete. AI analysis yielded no results."

    # 4. Process AI Results and Distribute Content
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
        if not category:
            log.warning(f"AI result for {original_candidate['title']} has no category. Skipping.")
            continue
            
        # Add to sent_links regardless of category to prevent reprocessing
        state["sent_links"][original_candidate['dedup_key']] = now_utc_iso

        if category in ["IGNORE", "SILENT"]:
            log.info(f"Offer '{original_candidate['title'][:40]}...' is '{category}'. Skipping publication.")
            # No further action, just recorded as sent
        elif category == "HIT":
            log.info(f"Offer '{original_candidate['title'][:40]}...' is a 'HIT'. Running full audit with Perplexity...")

            offer_price = original_candidate.get('price') or ai_result.get('price', 'Brak ceny')
            
            # Single call for combined extraction and audit
            audit_result = await run_full_perplexity_audit(
                title=original_candidate['title'],
                price=offer_price,
                link=original_candidate['link']
            )

            verdict = audit_result.get("verdict")

            if verdict in ["ERROR", "SKIPPED", "RISK"] or audit_result.get("telegram_message") == "NULL":
                log.info(f"Perplexity audit for '{original_candidate['title'][:40]}...' returned '{verdict}'. Not adding to digest.")
            else:
                log.info(f"Perplexity audit for '{original_candidate['title'][:40]}...' with verdict '{verdict}'. Adding to digest candidates.")
                
                existing_candidate_keys = {c.get('dedup_key') for c in state["digest_candidates"]}
                if original_candidate['dedup_key'] not in existing_candidate_keys:
                    # Combine original data with the rich data from the single audit call
                    candidate_to_add = {
                        **original_candidate,
                        **audit_result
                    }
                    state["digest_candidates"].append(candidate_to_add)
                    log.info(f"DIAGNOSTIC: Added to digest_candidates: {candidate_to_add}")
                else:
                    log.info(f"Offer '{original_candidate['title'][:40]}...' already exists in digest candidates (deduplicated).")
        else:
            log.warning(f"Unknown category '{category}' for offer '{original_candidate['title'][:40]}...'. Skipping.")

    log.info("Processing complete. Saving final state.")
    prune_sent_links(state)
    save_state_atomic(state, generation)
    return "Run complete."


async def master_scheduler():
    """Coordinates the main tasks based on a schedule."""
    now_utc = datetime.now(timezone.utc)
    log.info(f"Master scheduler running at {now_utc.isoformat()}")

    log.info("Scheduler: Kicking off ingestion and processing.")
    await process_and_publish_offers()
    
    # Perform delete sweep hourly
    state, generation = load_state()
    deleted_count = await perform_delete_sweep(state)
    if deleted_count > 0:
        save_state_atomic(state, generation)
        log.info(f"Scheduler: Performed delete sweep, {deleted_count} messages processed.")
    else:
        log.info("Scheduler: Delete sweep found no messages to process.")

    # Digest publication twice a day
    if now_utc.hour in [10, 20]: # Digest publication hours
        log.info(f"Scheduler: It's a digest hour ({now_utc.hour}:00 UTC). Publishing digest.")
        await publish_digest_async()
    else:
        log.info("Scheduler: Not a digest hour. Skipping digest.")
    
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