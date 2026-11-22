from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Optional

from ares.consts import EngagementResult
from sc2.position import Point2
from sc2.unit import Unit


class Mode(Enum):
    ATTACK = auto()
    DEFEND = auto()
    HOLD = auto()


@dataclass
class GroupOrders:
    label: str
    mode: Mode
    target: Point2
    hold_point: Point2
    front: Point2 = Point2((1.0, 0.0))
    bio_position: Optional[Point2] = None
    anchor: Optional[Point2] = None
    local_result: Optional[EngagementResult] = None
    unit_results: Optional[Dict[int, EngagementResult]] = None
    retreating: bool = False
    staging: Optional[Point2] = None
    pausing: bool = False
    hold_radius: float = 3.5

    @property
    def aggressive(self) -> bool:
        return self.mode in (Mode.ATTACK, Mode.DEFEND)

    def advance_result(self, unit: Unit) -> Optional[EngagementResult]:
        if self.unit_results is None:
            return self.local_result
        return self.unit_results.get(unit.tag)