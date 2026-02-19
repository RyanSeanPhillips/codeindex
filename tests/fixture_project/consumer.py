"""External consumer of DataManager — tests class-level impact."""

from pkg.manager import DataManager


def backup_workflow():
    """Uses DataManager.save_data from an external file."""
    mgr = DataManager()
    mgr.save_data("/backup/path")


def reset_all():
    """Uses DataManager.reset from an external file."""
    mgr = DataManager()
    mgr.reset()
