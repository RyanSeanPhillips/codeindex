"""Domain models."""

from dataclasses import dataclass


@dataclass
class User:
    """A user in the system."""
    name: str
    email: str
    active: bool = True

    def display_name(self) -> str:
        return self.name.title()

    def deactivate(self):
        self.active = False


@dataclass
class Config:
    """Application configuration."""
    debug: bool = False
    log_level: str = "INFO"
