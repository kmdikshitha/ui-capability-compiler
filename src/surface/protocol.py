"""The Surface Protocol.

Two methods of perception and action, plus teardown. Everything above this line
-- the agent loop, the compiler, the replay executor -- is written against this
and never against a browser. A desktop driver implementing observe()/act() over
the OS accessibility API would slot in without changing a single artifact.
"""

from __future__ import annotations

from src.types import Action, ActResult, Observation, Surface

__all__ = ["Surface", "Action", "ActResult", "Observation"]
