from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.ability_id import AbilityId
from sc2.unit import Unit
from sc2.units import Units
from sc2.position import Point2
from typing import FrozenSet, Set
from sc2.bot_ai import BotAI

from cython_extensions.units_utils import cy_closest_to, cy_sorted_by_distance_to
from cython_extensions.geometry import cy_distance_to_squared

from bot.utils.ares_helpers import can_build_here

WORKER_RUSH_HOME_RADIUS = 30.0
WORKER_RUSH_DANGER_RADIUS = 10.0
WORKER_RUSH_DANGER_RADIUS_SQ = WORKER_RUSH_DANGER_RADIUS * WORKER_RUSH_DANGER_RADIUS
WORKER_ATTACK_LEASH = 45.0
WORKER_ATTACK_LEASH_SQ = WORKER_ATTACK_LEASH * WORKER_ATTACK_LEASH
WORKER_RUSH_CLEAR_DELAY = 30.0
WALL_DANGER_DISTANCE_SQ = 64.0


def wall_as_fast_as_possible(self: BotAI):
    if self.army_advisor.is_wall_closed():
        return

    if not self.visible_enemy_units or not self.structures:
        return

    dist_sq = min(
        cy_distance_to_squared(cy_closest_to(e.position, self.structures).position, e.position)
        for e in self.visible_enemy_units
    )

    if dist_sq > WALL_DANGER_DISTANCE_SQ:
        depot_placement_positions: FrozenSet[Point2] = self.main_base_ramp.corner_depots
        depots: Units = self.structures.of_type({UnitTypeId.SUPPLYDEPOT, UnitTypeId.SUPPLYDEPOTLOWERED})

        if depots:
            depot_placement_positions: Set[Point2] = {d for d in depot_placement_positions if depots.closest_distance_to(d) > 1}

        if len(depot_placement_positions) > 0:
            for w in self.workers:
                if not w.is_constructing_scv and self.can_afford(UnitTypeId.SUPPLYDEPOT) and len(depot_placement_positions) > 0:
                    pos = depot_placement_positions.pop()
                    if can_build_here(self, pos, UnitTypeId.SUPPLYDEPOT):
                        w.build(UnitTypeId.SUPPLYDEPOT, pos)
                    else:
                        depot_placement_positions.add(pos)

        barracks_in_wall = False
        for b in self.structures(UnitTypeId.BARRACKS):
            if b.position == self.main_base_ramp.barracks_in_middle:
                barracks_in_wall = True

        if not barracks_in_wall:
            if can_build_here(self, self.main_base_ramp.barracks_in_middle, UnitTypeId.BARRACKS):
                for w in self.workers:
                    if not w.is_constructing_scv and self.can_afford(UnitTypeId.BARRACKS):
                        w.build(UnitTypeId.BARRACKS, self.main_base_ramp.barracks_in_middle)
                        break


def are_we_worker_rushed(self: BotAI):
    enemies: Units = self.visible_enemy_units.of_type({UnitTypeId.PROBE, UnitTypeId.SCV, UnitTypeId.DRONE})
    if enemies.empty:
        return 0, None, 0, None

    home: Units = self.structures.filter(lambda s: self.townhalls.closest_distance_to(s) <= WORKER_RUSH_HOME_RADIUS)
    if home.empty:
        return 0, None, 0, None

    dangerous_units = 0
    pathable_units = 0
    closest, closest_gap_sq = None, WORKER_RUSH_DANGER_RADIUS_SQ
    closest_inside, closest_inside_gap_sq = None, WORKER_RUSH_DANGER_RADIUS_SQ

    for e in enemies:
        closest_home = cy_closest_to(e.position, home)
        gap_sq = cy_distance_to_squared(e.position, closest_home.position)
        if gap_sq < WORKER_RUSH_DANGER_RADIUS_SQ:
            dangerous_units += 1
            if gap_sq < closest_gap_sq:
                closest, closest_gap_sq = e, gap_sq
            if e.position3d.z + 0.01 < self.townhalls.first.position3d.z:
                pathable_units += 1
            elif gap_sq < closest_inside_gap_sq:
                closest_inside, closest_inside_gap_sq = e, gap_sq

    return (
        dangerous_units,
        closest.position if closest is not None else None,
        dangerous_units - pathable_units,
        closest_inside.position if closest_inside is not None else None,
    )


def counter_worker_rush(self: BotAI, w, pos):
    if (w < 3 and not self.worker_rushed) or w == 0 or pos is None:
        return False

    mfs: Units = self.mineral_field.closer_than(12, self.start_location)
    self.worker_rushed = True
    counter = 0

    for i in cy_sorted_by_distance_to(self.workers, self.start_location):
        if i.is_constructing_scv:
            continue

        if i.health <= 6:
            if i.is_carrying_minerals:
                i(AbilityId.SMART, self.townhalls.first)
            else:
                if mfs.amount > 0:
                    mf: Unit = cy_closest_to(i.position, mfs)
                    i(AbilityId.SMART, mf)
            continue

        if i.weapon_cooldown > 5:
            if mfs.amount > 0:
                mf: Unit = cy_closest_to(i.position, mfs)
                i(AbilityId.SMART, mf)
            continue

        counter += 1
        if counter > w + 1:
            break
        i.attack(pos)

    return True


def pull_back_workers(self: BotAI):
    if not self.townhalls:
        return
    mfs: Units = self.mineral_field.closer_than(12, self.townhalls.first)
    if mfs.amount == 0:
        return
    for i in self.workers.idle:
        mf: Unit = cy_closest_to(i.position, mfs)
        i.gather(mf)


def recall_far_attackers(self: BotAI):
    if self.mineral_field.empty or not self.townhalls:
        return
    for worker in self.workers:
        if not worker.is_attacking:
            continue
        closest_th = cy_closest_to(worker.position, self.townhalls)
        if cy_distance_to_squared(closest_th.position, worker.position) > WORKER_ATTACK_LEASH_SQ:
            worker.gather(cy_closest_to(closest_th.position, self.mineral_field))


def worker_rush_defense(self: BotAI):
    w, pos, enemies_inside_wall, inside_pos = are_we_worker_rushed(self)

    if self.army_advisor.is_wall_closed():
        w, pos = enemies_inside_wall, inside_pos

    if counter_worker_rush(self, w, pos):
        if len(self.build_order) != 0:
            self.build_order = []

    recall_far_attackers(self)

    if self.worker_rushed:
        if w == 0:
            if self.worker_rush_clear_since is None:
                self.worker_rush_clear_since = self.time
            elif self.time - self.worker_rush_clear_since > WORKER_RUSH_CLEAR_DELAY:
                self.worker_rushed = False
                self.worker_rush_clear_since = None
        else:
            self.worker_rush_clear_since = None

    if self.worker_rushed:
        wall_as_fast_as_possible(self)
        if w == 0:
            pull_back_workers(self)