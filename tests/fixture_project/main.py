"""Main module for the fixture project."""

from pkg.utils import helper_function, unused_function
from pkg.models import User
from pkg.manager import DataManager


def main():
    """Entry point."""
    user = User(name="test", email="test@example.com")
    result = helper_function(user.name)
    process_result(result)
    return result


def load_workflow():
    """Uses DataManager.load_data from main.py."""
    mgr = DataManager()
    return mgr.load_data("/some/path")


def process_result(data):
    """Process a result from helper."""
    if data is None:
        return "empty"
    if isinstance(data, str):
        return data.upper()
    return str(data)


def dead_function():
    """This function is never called — should be flagged by DEAD_SYMBOL."""
    return 42


class Application:
    """Main application class."""

    def __init__(self):
        self.state = {}
        self.running = False

    def start(self):
        """Start the application."""
        self.running = True
        result = main()
        self._internal_setup()
        return result

    def stop(self):
        """Stop the application."""
        self.running = False

    def _internal_setup(self):
        """Private setup method."""
        self.state["initialized"] = True

    def a_very_long_method_that_exceeds_fifty_lines(self):
        """This method is artificially long to trigger LARGE_SYMBOL."""
        x = 1
        if x > 0:
            x += 1
        if x > 1:
            x += 1
        if x > 2:
            x += 1
        if x > 3:
            x += 1
        if x > 4:
            x += 1
        for i in range(10):
            if i % 2 == 0:
                x += i
            elif i % 3 == 0:
                x -= i
            else:
                x *= 2
        while x > 100:
            x -= 10
            if x < 50:
                break
        try:
            result = x / (x - 1)
        except ZeroDivisionError:
            result = 0
        except ValueError:
            result = -1
        if result > 0:
            pass
        if result > 1:
            pass
        if result > 2:
            pass
        if result > 3:
            pass
        if result > 4:
            pass
        if result > 5:
            pass
        if result > 6:
            pass
        if result > 7:
            pass
        if result > 8:
            pass
        if result > 9:
            pass
        if result > 10:
            pass
        if result > 11:
            pass
        if result > 12:
            pass
        if result > 13:
            pass
        if result > 14:
            pass
        if result > 15:
            pass
        if result > 16:
            pass
        if result > 17:
            pass
        if result > 18:
            pass
        return result
