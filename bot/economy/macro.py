import math
from typing import List, Optional, Tuple, FrozenSet, Set

from cython_extensions.units_utils import cy_closer_than, cy_closest_to
from cython_extensions.geometry import cy_distance_to_squared

from sc2.bot_ai import BotAI
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from bot.economy.addons import ADDON_OFFSET
from bot.consts import HARMLESS_TO_WORKERS
from bot.economy.repair import manage_repairs
from bot.utils.macro_utils import (
    ARMORY_MECH_SUPPLY,
    MECH_SUPPLY_FOR_TIER,
    can_build_structure,
    is_banking,
    mech_supply,
    mineral_income_outpacing_spend,
    get_safest_expansion,
    is_supply_critical,
    update_rally_points,
)


async def build_gas(self: BotAI) -> bool:
    for cc in self.townhalls.ready:
        vgs = cy_closer_than(self.vespene_geyser, 12, cc.position)
        for vg in vgs:
            if await self.can_place_single(UnitTypeId.REFINERY, vg.position):
                workers: Units = self.workers.gathering
                if workers:
                    worker: Unit = cy_closest_to(vg.position, workers)
                    worker.build_gas(vg)
                    return True
    return False


async def build_cc(self: BotAI, build_worker: Unit = None) -> bool:
    location: Point2 = await get_safest_expansion(self)
    if location is not None:
        worker: Unit = build_worker if build_worker is not None else self.select_build_worker(location)
        if worker is None:
            return False
        self.worker_assigned_to_expand[worker.tag] = worker
        worker.build(UnitTypeId.COMMANDCENTER, location)
        return True
    return False


async def find_production_spot(self: BotAI, type: UnitTypeId, lines: List[Tuple[Units, int]]) -> Optional[Point2]:
    places: List[Tuple[Point2, Point2]] = []
    for structures, shift in lines:
        for i in structures:
            for step in (3, -3):
                places.append((Point2((i.position.x + shift, i.position.y + step)), Point2((i.position.x + shift, i.position.y + 2 * step))))
    if not places:
        return None
    fits = await self.can_place(type, [spot for spot, _ in places] + [beyond for _, beyond in places])
    fitting = [places[k][0] for k in range(len(places)) if fits[k] and fits[len(places) + k]]
    if not fitting:
        return None
    room = await self.can_place(AbilityId.TERRANBUILD_SUPPLYDEPOT, [spot.offset(ADDON_OFFSET) for spot in fitting])
    return next((spot for spot, fits_addon in zip(fitting, room) if fits_addon), fitting[0])


async def smart_build(self: BotAI, type: UnitTypeId):
    if not self.can_afford(type) or self.tech_requirement_progress(type) != 1:
        return False

    prod_structures: Units = self.structures.of_type({UnitTypeId.BARRACKS, UnitTypeId.FACTORY, UnitTypeId.STARPORT})

    if prod_structures.amount == 0 and self.main_base_ramp.barracks_in_middle:
        if self.workers.amount == 0:
            return False
        worker: Unit = cy_closest_to(self.main_base_ramp.barracks_in_middle, self.workers)
        pos = self.main_base_ramp.barracks_correct_placement
        if self.enemy_race == Race.Zerg or self.enemy_race == Race.Protoss:
            pos = self.main_base_ramp.barracks_in_middle
        if self.mediator.can_place_structure(position=pos, structure_type=type):
            worker.build(type, pos)
            return True
        return False

    wall_pos = self.main_base_ramp.barracks_in_middle
    correct_pos = self.main_base_ramp.barracks_correct_placement
    non_wall_structures: Units = prod_structures.filter(
        lambda s: (wall_pos is None or cy_distance_to_squared(s.position, wall_pos) > 9)
        and (correct_pos is None or cy_distance_to_squared(s.position, correct_pos) > 9)
    )
    spot = await find_production_spot(self, type, [(non_wall_structures, 0), (prod_structures, -7), (prod_structures, 7), (prod_structures, -14), (prod_structures, 14)])
    if spot is None:
        return False
    return await self.build(type, near=spot, max_distance=0)


HALF_OFFSET = Point2((.5, .5))


async def smart_build_behind_mineral(self: BotAI, type: UnitTypeId, townhalls: Units = None, near_distance: float = 9, far_distance: float = 12):
    for cc in (self.townhalls.ready if townhalls is None else townhalls):
        mfs = cy_closer_than(self.mineral_field, 10, cc.position)
        if len(mfs) == 0:
            continue
        x = 0
        y = 0
        for i in mfs:
            x += i.position.x
            y += i.position.y
        x = x // len(mfs)
        y = y // len(mfs)
        for i in range(20):
            position = cc.position.towards_with_random_angle(Point2((x, y)), near_distance, (math.pi / 3))
            position_further = cc.position.towards_with_random_angle(Point2((x, y)), far_distance, (math.pi / 3))
            position = position.rounded.offset(HALF_OFFSET)
            position_further = position_further.rounded.offset(HALF_OFFSET)
            if self.mediator.can_place_structure(position=position, structure_type=type):
                await self.build(type, near=position, max_distance=4)
                return
            if self.mediator.can_place_structure(position=position_further, structure_type=type):
                await self.build(type, near=position_further, max_distance=4)
                return


CLEAR_OF_MINERALS = 6.0


async def smart_build_in_front_of_base(self: BotAI, type: UnitTypeId, townhalls: Units = None, near_distance: float = 8, far_distance: float = 10):
    for cc in (self.townhalls.ready if townhalls is None else townhalls):
        nearby_minerals = cy_closer_than(self.mineral_field, 14, cc.position)
        enemy_start = self.enemy_start_locations[0]
        for i in range(20):
            position = cc.position.towards_with_random_angle(enemy_start, near_distance, (math.pi / 3))
            position_further = cc.position.towards_with_random_angle(enemy_start, far_distance, (math.pi / 3))
            position = position.rounded.offset(HALF_OFFSET)
            position_further = position_further.rounded.offset(HALF_OFFSET)
            if not cy_closer_than(nearby_minerals, CLEAR_OF_MINERALS, position) and self.mediator.can_place_structure(position=position, structure_type=type):
                await self.build(type, near=position, max_distance=4, random_alternative=False)
                return
            if not cy_closer_than(nearby_minerals, CLEAR_OF_MINERALS, position_further) and self.mediator.can_place_structure(position=position_further, structure_type=type):
                await self.build(type, near=position_further, max_distance=4, random_alternative=False)
                return


def cancel_building(self: BotAI):
    for st in self.structures:
        if not st.is_ready and st.health_percentage < 0.1:
            st(AbilityId.CANCEL)


def resume_building_construction(self: BotAI):
    enemies: Units = self.visible_enemy_units.exclude_type(HARMLESS_TO_WORKERS)
    for i in self.structures_without_construction_SCVs:
        if (
            enemies.amount != 0
            and cy_distance_to_squared(cy_closest_to(i.position, enemies).position, i.position) < 64
        ) or (not self.army_advisor.is_wall_closed() and (self.worker_rushed or self.army_advisor.zergling_rushed)):
            return

    to_remove = []
    for k in self.worker_assigned_to_resume_building.keys():
        if self.structures.find_by_tag(k) is None or self.units.find_by_tag(self.worker_assigned_to_resume_building[k]) is None:
            to_remove.append(k)
    for i in to_remove:
        self.worker_assigned_to_resume_building.pop(i, None)

    for i in self.structures_without_construction_SCVs:
        if i.tag in self.worker_assigned_to_resume_building.keys():
            continue
        if self.workers.gathering.amount == 0:
            continue
        worker = cy_closest_to(i.position, self.workers.gathering)
        self.worker_assigned_to_resume_building[i.tag] = worker.tag
        worker(AbilityId.SMART, i)


MAX_BARRACKS = 8
MAX_FACTORIES = 2
MAX_STARPORTS = 2
BANK_MINERALS = 1000
BANK_GAS = 350
END_GAME_SUPPLY = 100
TURRET_BASE_RADIUS = 15.0
MECH_BARRACKS_BY_BASES = {1: 1, 2: 1, 3: 2, 4: 3}


def production_targets(self: BotAI) -> dict:
    bases = self.townhalls.amount
    advisor = self.army_advisor
    have = {
        UnitTypeId.BARRACKS: self.structures(UnitTypeId.BARRACKS).amount + self.structures(UnitTypeId.BARRACKSFLYING).amount,
        UnitTypeId.FACTORY: self.structures(UnitTypeId.FACTORY).amount + self.structures(UnitTypeId.FACTORYFLYING).amount,
        UnitTypeId.STARPORT: self.structures(UnitTypeId.STARPORT).amount + self.structures(UnitTypeId.STARPORTFLYING).amount,
    }
    starports = 2 if bases >= 4 else (1 if bases >= 2 or advisor.starport_now else 0)
    factories = 2 if bases >= 3 and (advisor.max_tanks + advisor.max_cyclones) > 10 else (1 if bases >= 1 else 0)
    barracks = 6 if bases >= 4 else (5 if bases >= 3 else (2 if bases >= 2 else (1 if bases >= 1 else 0)))
    if advisor.mech_focus:
        barracks = MECH_BARRACKS_BY_BASES.get(min(bases, 4), 0)
    targets = {UnitTypeId.STARPORT: starports, UnitTypeId.FACTORY: factories, UnitTypeId.BARRACKS: barracks}

    outpacing = mineral_income_outpacing_spend(self)
    if outpacing and UnitTypeId.COMMANDCENTER not in self.build_order:
        targets[UnitTypeId.BARRACKS] = max(targets[UnitTypeId.BARRACKS], have[UnitTypeId.BARRACKS] + 1)

    if self.supply_used >= END_GAME_SUPPLY and self.minerals >= BANK_MINERALS:
        gas_banking = self.vespene >= BANK_GAS
        tanks = self.units.of_type({UnitTypeId.SIEGETANK, UnitTypeId.SIEGETANKSIEGED}).amount
        cyclones = self.units(UnitTypeId.CYCLONE).amount
        targets[UnitTypeId.BARRACKS] = max(targets[UnitTypeId.BARRACKS], have[UnitTypeId.BARRACKS] + 1)
        if gas_banking and (tanks < advisor.max_tanks or cyclones < advisor.max_cyclones):
            targets[UnitTypeId.FACTORY] = max(targets[UnitTypeId.FACTORY], have[UnitTypeId.FACTORY] + 1)
        if gas_banking:
            targets[UnitTypeId.STARPORT] = max(targets[UnitTypeId.STARPORT], have[UnitTypeId.STARPORT] + 1)
    caps = {UnitTypeId.BARRACKS: MAX_BARRACKS, UnitTypeId.FACTORY: MAX_FACTORIES, UnitTypeId.STARPORT: MAX_STARPORTS}
    return {t: min(caps[t], amount) for t, amount in targets.items()}


async def build_production_buildings(self: BotAI):
    targets = production_targets(self)
    for building, flying in ((UnitTypeId.STARPORT, UnitTypeId.STARPORTFLYING), (UnitTypeId.FACTORY, UnitTypeId.FACTORYFLYING),
                             (UnitTypeId.BARRACKS, UnitTypeId.BARRACKSFLYING)):
        if can_build_structure(self, building, flying, targets[building]):
            await smart_build(self, building)


async def build_turrets(self: BotAI):
    per_base = self.army_advisor.turrets_per_base
    if per_base <= 0 or self.townhalls.amount == 0:
        return
    if not self.structures(UnitTypeId.ENGINEERINGBAY).ready:
        if can_build_structure(self, UnitTypeId.ENGINEERINGBAY, None, 1):
            await smart_build_behind_mineral(self, UnitTypeId.ENGINEERINGBAY)
        return
    if self.already_pending(UnitTypeId.MISSILETURRET) > 0 or not self.can_afford(UnitTypeId.MISSILETURRET):
        return
    for cc in self.townhalls.ready:
        if self.turret_backoff.get(cc.tag, 0.0) > self.time:
            continue
        if len(cy_closer_than(self.structures(UnitTypeId.MISSILETURRET), TURRET_BASE_RADIUS, cc.position)) < per_base:
            self.turret_backoff[cc.tag] = self.time + 4.0
            await smart_build_behind_mineral(self, UnitTypeId.MISSILETURRET, townhalls=Units([cc], self), near_distance=6, far_distance=9)
            return


async def macro(self: BotAI):
    cancel_building(self)
    manage_repairs(self)
    resume_building_construction(self)
    update_rally_points(self)

    if self.workers.amount == 0:
        return

    await build_production_buildings(self)
    await build_turrets(self)

    if self.townhalls.amount >= 3 and can_build_structure(self, UnitTypeId.ENGINEERINGBAY, None, 2):
        await smart_build_behind_mineral(self, UnitTypeId.ENGINEERINGBAY)

    mech = mech_supply(self)
    first_bio_upgrade_done = (
        self.already_pending_upgrade(UpgradeId.TERRANINFANTRYARMORSLEVEL1) == 1
        or self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL1) == 1
    )
    if (
        self.townhalls.amount >= 2 and can_build_structure(self, UnitTypeId.ARMORY, None, 1)
        and (
            mech.armor >= ARMORY_MECH_SUPPLY
            or (
                self.army_advisor.mech_focus
                and self.structures(UnitTypeId.STARPORT).amount + self.already_pending(UnitTypeId.STARPORT) >= 1
                and first_bio_upgrade_done
            )
        )
    ):
        await smart_build_behind_mineral(self, UnitTypeId.ARMORY)

    if mech.armor >= MECH_SUPPLY_FOR_TIER[2] and is_banking(self) and can_build_structure(self, UnitTypeId.ARMORY, None, 2):
        await smart_build_behind_mineral(self, UnitTypeId.ARMORY)

    if (self.already_pending_upgrade(UpgradeId.TERRANINFANTRYARMORSLEVEL1) > 0.3 or self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL1) > 0.3) and can_build_structure(self, UnitTypeId.ARMORY, None, 1):
        await smart_build_behind_mineral(self, UnitTypeId.ARMORY)

    if self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL3) == 1 and can_build_structure(self, UnitTypeId.ARMORY, None, 2):
        await smart_build_behind_mineral(self, UnitTypeId.ARMORY)

    if (self.already_pending_upgrade(UpgradeId.TERRANINFANTRYARMORSLEVEL3) == 1 or self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL3) == 1) and can_build_structure(self, UnitTypeId.FUSIONCORE, None, 1):
        await smart_build_behind_mineral(self, UnitTypeId.FUSIONCORE)

    producer_idle: bool = self.structures(UnitTypeId.BARRACKS).ready.idle.amount > 0
    holding_for_units: bool = self.supply_army == 0
    holding_for_enemy_all_in: bool = self.townhalls.amount >= 2 and self.army_advisor.enemy_likely_one_base

    if (
        self.can_afford(UnitTypeId.COMMANDCENTER)
        and self.townhalls.amount < 8
        and (self.already_pending(UnitTypeId.COMMANDCENTER) == 0 or self.minerals > 1500)
        and (not is_supply_critical(self) or self.minerals > 1500)
        and (not holding_for_enemy_all_in or self.minerals > 1500)
    ):
        workers_per_base = self.workers.amount / max(1, self.townhalls.amount)
        crowded = workers_per_base >= 18
        past_three_bases = self.townhalls.amount >= 3

        if (
            (not holding_for_units and not crowded and not past_three_bases)
            or crowded
            or (past_three_bases and self.minerals >= 500)
            or self.minerals > 1500
        ):
            await build_cc(self)

    refineries = self.structures(UnitTypeId.REFINERY)
    active_refineries = 0
    for r in refineries:
        if r.vespene_contents > 20:
            active_refineries += 1
    for w in self.workers:
        if isinstance(w.order_target, int) and self.vespene_geyser.find_by_tag(w.order_target) is not None:
            active_refineries += 1

    if (self.worker_rushed or self.army_advisor.zergling_rushed) and self.structures.of_type(UnitTypeId.REFINERY).amount != 0:
        if self.structure_type_build_progress(UnitTypeId.REFINERY) < 1:
            self.structures.of_type(UnitTypeId.REFINERY).first(AbilityId.CANCEL)

    GAS_BANK_HIGH = 600
    GAS_BANK_LOW = 400
    if self.gas_bank_high:
        self.gas_bank_high = self.vespene > GAS_BANK_LOW
    else:
        self.gas_bank_high = self.vespene > GAS_BANK_HIGH

    supply_critical_for_gas = is_supply_critical(self) and self.minerals <= 1200

    if self.townhalls.amount >= 1 and active_refineries < 1 and self.can_afford(UnitTypeId.REFINERY) and self.structures(UnitTypeId.BARRACKS).amount > 0 and not self.worker_rushed and not self.army_advisor.zergling_rushed and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 2 and active_refineries < 2 and self.can_afford(UnitTypeId.REFINERY) and (self.structures(UnitTypeId.FACTORY).amount > 0 or self.already_pending(UnitTypeId.FACTORY) > 0) and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 2 and self.structures(UnitTypeId.STARPORT).amount > 0 and active_refineries < 3 and self.can_afford(UnitTypeId.REFINERY) and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 3 and active_refineries < 4 and self.can_afford(UnitTypeId.REFINERY) and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 4 and active_refineries < 6 and self.can_afford(UnitTypeId.REFINERY) and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 5 and active_refineries < 7 and self.can_afford(UnitTypeId.REFINERY) and not self.gas_bank_high and not supply_critical_for_gas:
        await build_gas(self)
    if self.townhalls.amount >= 5 and active_refineries < 8 and self.can_afford(UnitTypeId.REFINERY) and self.minerals > 1200 and not self.gas_bank_high:
        await build_gas(self)