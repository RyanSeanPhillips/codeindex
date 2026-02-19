"""Module with signal-like .connect() patterns for testing signal-aware parsing."""


class Signal:
    """Fake signal class for testing."""
    def connect(self, slot):
        pass

    def emit(self, *args):
        pass


class Widget:
    """Widget with signal connections."""

    def __init__(self):
        self.clicked = Signal()
        self.data_changed = Signal()
        self.clicked.connect(self.on_clicked)
        self.data_changed.connect(self.on_data_changed)

    def on_clicked(self):
        """Handler for clicked signal."""
        return "clicked"

    def on_data_changed(self):
        """Handler for data_changed signal."""
        return "changed"

    def setup_connections(self):
        """Set up additional connections."""
        self.clicked.connect(self.handle_click)

    def handle_click(self):
        """Another click handler."""
        return "handled"
