"""A manager class for testing class-level impact analysis."""


class DataManager:
    """Manages data operations — used from multiple files."""

    def __init__(self):
        self.data = []

    def load_data(self, path):
        """Load data from a path."""
        self.data = [path]
        return self.data

    def save_data(self, path):
        """Save data to a path."""
        return len(self.data)

    def reset(self):
        """Reset all data."""
        self.data = []

    def _internal_helper(self):
        """Internal helper — called only within class."""
        return len(self.data)
