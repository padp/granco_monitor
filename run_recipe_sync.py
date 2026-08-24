"""Entry point: python run_recipe_sync.py (run alongside run_collector.py, run_plex_sync.py, run_publisher.py)"""
from recipe_sync.sync import run

if __name__ == "__main__":
    run()
