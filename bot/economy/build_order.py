from bot.economy.addons import points_to_build_addon
from bot.economy.macro import smart_build
from bot.economy.macro import build_gas
from bot.economy.macro import build_cc
from bot.utils.ares_helpers import can_build_here

from cython_extensions.units_utils import cy_closest_to

from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.ability_id import AbilityId
from sc2.unit import Unit
from sc2.units import Units
from sc2.position import Point2
from typing import FrozenSet, Set
from sc2.bot_ai import BotAI
from sc2.data import Race


def maybe_prioritize_factory(self: BotAI):
    if self.factory_prioritized or self.enemy_race != Race.Terran:
        return
    self.factory_prioritized = True
    if UnitTypeId.FACTORY not in self.build_order:
        return
    self.build_order.remove(UnitTypeId.FACTORY)

    insert_at = 0
    for prereq in (UnitTypeId.BARRACKS, UnitTypeId.REFINERY):
        if prereq in self.build_order:
            insert_at = max(insert_at, self.build_order.index(prereq) + 1)
    self.build_order.insert(insert_at, UnitTypeId.FACTORY)


BUILD_ORDER_TIMEOUT = 360.0


async def early_build_order(self: BotAI):
    if len(self.build_order) == 0:
        self.build_order_critical_worker = None
        return

    maybe_prioritize_factory(self)

    if self.time > BUILD_ORDER_TIMEOUT:
        self.build_order = []
        return

    if self.structures_without_construction_SCVs.amount > 0:
        self.build_order = []
        return

    depot_placement_positions: FrozenSet[Point2] = self.main_base_ramp.corner_depots
    depots: Units = self.structures.of_type({UnitTypeId.SUPPLYDEPOT, UnitTypeId.SUPPLYDEPOTLOWERED})

    if depots:
        depot_placement_positions: Set[Point2] = {d for d in depot_placement_positions if depots.closest_distance_to(d) > 1}

    if self.build_order[0] == UnitTypeId.SUPPLYDEPOT and self.minerals > 25 and self.time > 5:
        location: Point2 = next(iter(depot_placement_positions))
        if location is not None:
            if self.move_to_depot == -1 or self.workers.find_by_tag(self.move_to_depot) is None:
                worker: Unit = self.select_build_worker(location)
                if worker is None:
                    return
                worker.move(location)
                self.move_to_depot = worker.tag
                self.build_order_critical_worker = worker.tag
            else:
                self.workers.find_by_tag(self.move_to_depot).move(location)

    if self.can_afford(UnitTypeId.SUPPLYDEPOT) and self.build_order[0] == UnitTypeId.SUPPLYDEPOT:
        target_depot_location: Point2 = depot_placement_positions.pop()
        if can_build_here(self, target_depot_location, UnitTypeId.SUPPLYDEPOT):
            if await self.build(UnitTypeId.SUPPLYDEPOT, near=target_depot_location, build_worker=self.workers.find_by_tag(self.move_to_depot)):
                self.build_order.pop(0)
                self.move_to_depot = -1
                self.build_order_critical_worker = None
        return

    if self.build_order[0] == UnitTypeId.BARRACKS and self.minerals > 75 and self.time > 1:
        location = self.main_base_ramp.barracks_correct_placement
        if self.enemy_race == Race.Zerg or self.enemy_race == Race.Protoss:
            location = self.main_base_ramp.barracks_in_middle
        if location is not None:
            worker: Unit = self.workers.find_by_tag(self.build_order_critical_worker)
            if worker is None:
                worker = cy_closest_to(location, self.workers)
                if worker is None:
                    return
                self.build_order_critical_worker = worker.tag
            worker.move(location)

    if self.can_afford(UnitTypeId.BARRACKS) and self.build_order[0] == UnitTypeId.BARRACKS and self.tech_requirement_progress(UnitTypeId.BARRACKS) == 1:
        if await smart_build(self, UnitTypeId.BARRACKS):
            self.build_order.pop(0)
            self.build_order_critical_worker = None
        return

    if self.can_afford(UnitTypeId.FACTORY) and self.build_order[0] == UnitTypeId.FACTORY and self.tech_requirement_progress(UnitTypeId.FACTORY) == 1:
        if await smart_build(self, UnitTypeId.FACTORY):
            self.build_order.pop(0)
        return

    if self.can_afford(UnitTypeId.REFINERY) and self.build_order[0] == UnitTypeId.REFINERY:
        if await build_gas(self):
            self.build_order.pop(0)
        return

    if self.build_order[0] == UnitTypeId.COMMANDCENTER and self.minerals > 250:
        location: Point2 = await self.get_next_expansion()
        if location is not None:
            worker: Unit = self.workers.find_by_tag(self.build_order_critical_worker)
            if worker is None:
                worker = self.select_build_worker(location)
                if worker is None:
                    return
                self.build_order_critical_worker = worker.tag
            worker.move(location)

    if self.can_afford(UnitTypeId.COMMANDCENTER) and self.build_order[0] == UnitTypeId.COMMANDCENTER:
        if await build_cc(self, build_worker=self.workers.find_by_tag(self.build_order_critical_worker)):
            self.build_order.pop(0)
            self.build_order_critical_worker = None
        return

    orbital_tech_requirement: float = self.tech_requirement_progress(UnitTypeId.ORBITALCOMMAND)
    if orbital_tech_requirement == 1 and self.can_afford(UnitTypeId.ORBITALCOMMAND) and self.build_order[0] == UnitTypeId.ORBITALCOMMAND:
        cc: Unit
        for cc in self.townhalls(UnitTypeId.COMMANDCENTER).ready:
            if not cc.is_using_ability(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND):
                cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND)
            self.build_order.pop(0)
            return

    if self.can_afford(UnitTypeId.BARRACKSREACTOR) and self.build_order[0] == UnitTypeId.BARRACKSREACTOR:
        bar: Unit
        for bar in self.structures(UnitTypeId.BARRACKS).ready.idle:
            if not bar.has_add_on:
                addon_points = points_to_build_addon(bar.position)
                if all(self.in_map_bounds(addon_point) and self.in_placement_grid(addon_point) and self.in_pathing_grid(addon_point) for addon_point in addon_points):
                    bar.build(UnitTypeId.BARRACKSREACTOR)
                    self.build_order.pop(0)
                    return