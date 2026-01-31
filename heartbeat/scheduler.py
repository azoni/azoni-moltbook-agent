"""
Heartbeat scheduler for Azoni Moltbook Agent.

Runs the agent periodically when autonomous mode is enabled.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agent import run_agent
from config.firebase import get_firestore, MOLTBOOK_CONFIG, MOLTBOOK_STATE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_autonomous_mode() -> bool:
    """Check if autonomous mode is enabled in config."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            return config_doc.to_dict().get("autonomous_mode", False)
        return False
    except Exception as e:
        logger.error(f"Error checking autonomous mode: {e}")
        return False


def get_heartbeat_interval() -> int:
    """Get heartbeat interval from config (in hours)."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            return config_doc.to_dict().get("heartbeat_interval_hours", 4)
        return 4
    except Exception as e:
        logger.error(f"Error getting heartbeat interval: {e}")
        return 4


def heartbeat_job():
    """
    The heartbeat job that runs periodically.
    
    Only executes if autonomous mode is enabled.
    """
    logger.info(f"Heartbeat triggered at {datetime.now()}")
    
    # Check if autonomous mode is enabled
    if not check_autonomous_mode():
        logger.info("Autonomous mode is disabled, skipping heartbeat")
        return
    
    logger.info("Autonomous mode enabled, running agent...")
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Regular heartbeat check - observe the community and engage if appropriate"
        )
        
        decision = result.get("decision", {})
        action = decision.get("action", "unknown")
        executed = result.get("executed", False)
        
        logger.info(f"Heartbeat completed: action={action}, executed={executed}")
        
        if result.get("error"):
            logger.error(f"Heartbeat error: {result['error']}")
        
    except Exception as e:
        logger.error(f"Heartbeat job failed: {e}")
        
        # Log error to Firestore
        try:
            db = get_firestore()
            db.collection(MOLTBOOK_STATE).document("agent").set({
                "last_heartbeat_error": str(e),
                "last_heartbeat_error_at": datetime.now()
            }, merge=True)
        except:
            pass


def run_scheduler():
    """
    Start the heartbeat scheduler.
    """
    scheduler = BlockingScheduler()
    
    # Get interval from config
    interval_hours = get_heartbeat_interval()
    
    logger.info(f"Starting heartbeat scheduler with {interval_hours}h interval")
    
    # Add the job
    scheduler.add_job(
        heartbeat_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="moltbook_heartbeat",
        name="Moltbook Heartbeat",
        replace_existing=True
    )
    
    # Also run once on startup (after a short delay)
    scheduler.add_job(
        heartbeat_job,
        trigger="date",
        run_date=datetime.now(),
        id="moltbook_startup",
        name="Moltbook Startup Check"
    )
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    run_scheduler()
