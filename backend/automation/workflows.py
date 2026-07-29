"""Declarative workflow definitions without an execution engine dependency."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    name: str
    job_name: str
    order: int


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    name: str
    version: int
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        orders = [step.order for step in self.steps]
        if len(orders) != len(set(orders)):
            raise ValueError("workflow step order values must be unique")
