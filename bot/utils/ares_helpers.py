from __future__ import annotations

from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI


def can_build_here(
    ai: BotAI,
    position: Point2,
    structure_type: UnitTypeId,
    include_addon: bool = False,
) -> bool:
    return ai.mediator.can_place_structure(
        position=position,
        structure_type=structure_type,
        include_addon=include_addon,
    )


def request_depot_placement(
    ai: BotAI,
    base_location: Point2,
) -> Point2 | None:
    return ai.mediator.request_building_placement(
        base_location=base_location,
        structure_type=UnitTypeId.SUPPLYDEPOT,
        find_alternative=False,
        reserve_placement=False,
    )