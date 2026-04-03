from __future__ import annotations

from abc import ABC, abstractmethod

from sts_bot.models import GameAction, GameState, RunSummary


class GameAdapter(ABC):
    @abstractmethod
    def start_run(self) -> None:
        """Reset adapter state and begin a new run."""

    @abstractmethod
    def current_state(self) -> GameState:
        """Return the current observable game state."""

    @abstractmethod
    def available_actions(self) -> list[GameAction]:
        """Return the actions the policy may choose from."""

    @abstractmethod
    def apply_action(self, action: GameAction) -> None:
        """Apply a selected action."""

    @abstractmethod
    def is_run_over(self) -> bool:
        """Return whether the run has ended."""

    @abstractmethod
    def run_summary(self) -> RunSummary:
        """Return summary after a run is complete."""

