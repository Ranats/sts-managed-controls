from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import replace

from sts_bot.adapters.base import GameAdapter
from sts_bot.logging import RunLogger
from sts_bot.models import ActionEvaluation, RunSummary
from sts_bot.policy import Policy


@dataclass(slots=True)
class EpisodeResult:
    run_id: str
    summary: RunSummary


class AutoplayEngine:
    def __init__(self, adapter: GameAdapter, policy: Policy, logger: RunLogger) -> None:
        self.adapter = adapter
        self.policy = policy
        self.logger = logger

    def run_episode(self, max_steps: int | None = None) -> EpisodeResult:
        self.adapter.start_run()
        self.logger.start_run()
        steps = 0
        empty_action_observations = 0

        while not self.adapter.is_run_over():
            if max_steps is not None and steps >= max_steps:
                raise RuntimeError(f"Episode exceeded max_steps={max_steps}.")
            state = self.adapter.current_state()
            actions = self.adapter.available_actions()
            if not actions:
                empty_action_observations += 1
                if empty_action_observations <= 3:
                    time.sleep(0.35)
                    continue
                raise RuntimeError(f"No actions available on screen={state.screen.value}.")
            empty_action_observations = 0
            evaluations = self._evaluate_actions(state, actions)
            if evaluations:
                best = max(evaluations, key=lambda item: (item.score, -len(item.action_label)))
                action = next(action for action in actions if action.label == best.action_label)
            else:
                action = self.policy.choose_action(state, actions)
            current_intent = getattr(self.policy, "current_run_intent", lambda: None)()
            if current_intent is not None:
                state = replace(state, run_intent=current_intent)
            self.logger.log_decision(state, action, evaluations=evaluations)
            self.adapter.apply_action(action)
            steps += 1

        summary = self.adapter.run_summary()
        run_id = self.logger.finish_run(summary)
        return EpisodeResult(run_id=run_id, summary=summary)

    def _evaluate_actions(self, state, actions) -> list[ActionEvaluation] | None:
        evaluate = getattr(self.policy, "evaluate_actions", None)
        if callable(evaluate):
            return evaluate(state, actions)
        return None
