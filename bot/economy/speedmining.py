from typing import Dict, Set

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from cython_extensions.units_utils import cy_closer_than, cy_closest_to, cy_sorted_by_distance_to
from cython_extensions.geometry import cy_distance_to_squared, cy_towards

from bot.economy.worker_micro import base_is_threatened

SPEEDMINING_DISTANCE = 1.8
SPEEDMINING_DISTANCE_SQ = SPEEDMINING_DISTANCE * SPEEDMINING_DISTANCE


def micro_worker(self: BotAI) -> None:
    if self.townhalls.ready.amount <= 0:
        return

    safe_townhalls = self.townhalls.ready.filter(lambda t: not base_is_threatened(self, t.position))
    idle_pool = safe_townhalls if safe_townhalls else self.townhalls.ready

    for unit in self.workers:
        if unit.is_idle and unit.tag not in self.oracle_fleeing and unit.tag not in self.threat_fleeing:
            townhall = cy_closest_to(unit.position, idle_pool)
            patch = cy_closest_to(townhall.position, self.mineral_field)
            unit.gather(patch)
            continue

        if (not unit.is_carrying_minerals and not unit.is_carrying_vespene
                and not unit.is_constructing_scv and not unit.is_repairing
                and len(unit.orders) == 1
                and isinstance(unit.order_target, int)
                and unit.order_target not in self.resource_by_tag):
            townhall = cy_closest_to(unit.position, idle_pool)
            patch = cy_closest_to(townhall.position, self.mineral_field)
            if patch:
                unit.gather(patch)
                continue

        if len(unit.orders) == 1:
            target = None
            if unit.is_returning and not unit.is_carrying_vespene:
                target = cy_closest_to(unit.position, self.townhalls.ready)
                move_target = Point2(cy_towards(target.position, unit.position, target.radius + unit.radius))
            elif unit.is_gathering:
                target: Unit = self.resource_by_tag.get(unit.order_target)
                if target and not target.is_vespene_geyser and target.position in self.speedmining_positions.keys():
                    move_target = self.speedmining_positions[target.position]
                else:
                    target = None
            if target and not target.is_vespene_geyser:
                min_dist_sq = 4 * unit.radius * unit.radius
                if min_dist_sq < cy_distance_to_squared(unit.position, move_target) < SPEEDMINING_DISTANCE_SQ:
                    unit.move(move_target)
                    unit(AbilityId.SMART, target, True)


def handle_refineries(self: BotAI, step: int):
    for r in self.gas_buildings.ready:
        if not r.tag in self.refineries_age.keys():
            self.refineries_age[r.tag] = step

    to_remove = []
    for k in self.refineries_age.keys():
        if self.gas_buildings.ready.find_by_tag(k) is None:
            to_remove.append(k)
    for i in to_remove:
        self.refineries_age.pop(i, None)

    under_rush: bool = self.worker_rushed or self.army_advisor.zergling_rushed
    for r in self.gas_buildings.ready:
        target_harvesters = 1 if under_rush else r.ideal_harvesters
        if r.assigned_harvesters < target_harvesters and step - self.refineries_age[r.tag] > 6:
            workers = cy_closer_than(self.workers, 10, r.position)
            if workers:
                for w in workers:
                    if not w.is_carrying_minerals and not w.is_carrying_vespene:
                        w.gather(r)
                        return

        if r.assigned_harvesters > target_harvesters or self.workers.amount <= 6:
            workers = cy_closer_than(self.workers, 2, r.position)
            if workers:
                for w in workers:
                    if w.is_carrying_vespene:
                        w.gather(cy_closest_to(w.position, self.resources.mineral_field), True)
                        return


def dispatch_workers(self: BotAI):
    keys_to_delete = []
    for key in self.townhall_saturations.keys():
        if self.townhalls.ready.find_by_tag(key) is None:
            keys_to_delete.append(key)
    for i in keys_to_delete:
        del self.townhall_saturations[i]

    maxes: Dict = {}
    for cc in self.townhalls.ready:
        if not cc.tag in self.townhall_saturations.keys():
            self.townhall_saturations[cc.tag] = []
        if len(self.townhall_saturations[cc.tag]) >= 40:
            self.townhall_saturations[cc.tag].pop(0)
        self.townhall_saturations[cc.tag].append(cc.assigned_harvesters)
        maxes[cc.tag] = max(self.townhall_saturations[cc.tag])

    for key in maxes.keys():
        cc1 = self.townhalls.ready.find_by_tag(key)
        if maxes[key] + 1 > cc1.ideal_harvesters:
            for key2 in maxes.keys():
                if key2 == key:
                    continue
                cc2 = self.townhalls.ready.find_by_tag(key2)
                if maxes[key2] + 1 < cc2.ideal_harvesters and not base_is_threatened(self, cc2.position):
                    for w in self.workers.closer_than(10, cc1).gathering:
                        if self.mineral_field.closer_than(10, cc1).find_by_tag(w.order_target) is not None:
                            w.gather(w.position.closest(self.mineral_field.closer_than(10, cc2)))
                            maxes[key] -= 1
                            for i in range(len(self.townhall_saturations[key])):
                                self.townhall_saturations[key][i] -= 1
                            maxes[key2] += 1
                            for i in range(len(self.townhall_saturations[key2])):
                                self.townhall_saturations[key2][i] += 1
                            break


def split_workers(self: BotAI) -> None:
    minerals = self.expansion_locations_dict[self.start_location].mineral_field
    sorted_minerals = cy_sorted_by_distance_to(minerals, self.start_location)
    self.close_minerals = {m.tag for m in sorted_minerals[0:4]}
    assigned: Set[int] = set()
    for i in range(self.workers.amount):
        patch = sorted_minerals[i % len(sorted_minerals)]
        if i < len(sorted_minerals):
            worker = cy_closest_to(patch.position, self.workers.tags_not_in(assigned))
        else:
            worker = self.workers.tags_not_in(assigned).furthest_to(patch)
        worker.gather(patch)
        assigned.add(worker.tag)


def mine(self: BotAI, iteration):
    dispatch_workers(self)
    micro_worker(self)
    handle_refineries(self, iteration)