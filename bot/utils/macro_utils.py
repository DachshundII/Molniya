import math
from collections import Counter
from typing import NamedTuple, Optional, Set

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from cython_extensions.units_utils import cy_closer_than, cy_closest_to
from cython_extensions.geometry import cy_distance_to_squared, cy_towards
from cython_extensions.general_utils import cy_unit_pending


def can_build_structure(self: BotAI, structure_type, fly_type, amount):
    if fly_type is not None:
        return (
            self.structures(structure_type).amount
            + self.already_pending(structure_type)
            + self.structures(fly_type).amount
            < amount
            and self.can_afford(structure_type)
            and self.tech_requirement_progress(structure_type) == 1
        )
    return (
        self.structures(structure_type).amount + self.already_pending(structure_type) < amount
        and self.can_afford(structure_type)
        and self.tech_requirement_progress(structure_type) == 1
    )


def handle_depot_status(self: BotAI):
    visible_ground_enemies: Units = self.visible_enemy_units.not_flying.exclude_type(
        {UnitTypeId.REAPER, UnitTypeId.COLOSSUS}
    )
    if visible_ground_enemies.amount == 0:
        for depo in self.structures(UnitTypeId.SUPPLYDEPOT).ready:
            depo(AbilityId.MORPH_SUPPLYDEPOT_LOWER)
    else:
        for depo in self.structures(UnitTypeId.SUPPLYDEPOT).ready:
            closest_enemy = cy_closest_to(depo.position, visible_ground_enemies)
            if cy_distance_to_squared(closest_enemy.position, depo.position) >= 144:
                depo(AbilityId.MORPH_SUPPLYDEPOT_LOWER)
        for depo in self.structures(UnitTypeId.SUPPLYDEPOTLOWERED).ready:
            closest_enemy = cy_closest_to(depo.position, visible_ground_enemies)
            if cy_distance_to_squared(closest_enemy.position, depo.position) < 144:
                blocking: Units = self.units.filter(
                    lambda u: cy_distance_to_squared(u.position, depo.position) < 2.25
                )
                for u in blocking:
                    u.move(Point2(cy_towards(u.position, self.start_location, 2)))
                depo(AbilityId.MORPH_SUPPLYDEPOT_RAISE)


VEHICLE_TYPES = frozenset({
    UnitTypeId.SIEGETANK, UnitTypeId.SIEGETANKSIEGED, UnitTypeId.CYCLONE,
    UnitTypeId.THOR, UnitTypeId.THORAP, UnitTypeId.HELLION, UnitTypeId.HELLIONTANK,
})
SHIP_TYPES = frozenset({
    UnitTypeId.VIKINGFIGHTER, UnitTypeId.BANSHEE, UnitTypeId.LIBERATOR,
    UnitTypeId.LIBERATORAG, UnitTypeId.BATTLECRUISER,
})
MINE_TYPES = frozenset({UnitTypeId.WIDOWMINE, UnitTypeId.WIDOWMINEBURROWED})
MECH_SUPPLY_FOR_TIER = (9, 15, 24)
ARMORY_MECH_SUPPLY = 9
UPGRADE_BANK_MINERALS = 800
UPGRADE_BANK_GAS = 300
VEHICLE_WEAPONS = (
    UpgradeId.TERRANVEHICLEWEAPONSLEVEL1,
    UpgradeId.TERRANVEHICLEWEAPONSLEVEL2,
    UpgradeId.TERRANVEHICLEWEAPONSLEVEL3,
)
VEHICLE_AND_SHIP_ARMOR = (
    UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL1,
    UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL2,
    UpgradeId.TERRANVEHICLEANDSHIPARMORSLEVEL3,
)
SHIP_WEAPONS = (
    UpgradeId.TERRANSHIPWEAPONSLEVEL1,
    UpgradeId.TERRANSHIPWEAPONSLEVEL2,
    UpgradeId.TERRANSHIPWEAPONSLEVEL3,
)


class MechSupply(NamedTuple):
    vehicles: float
    ships: float
    armor: float


def mech_supply(self: BotAI) -> MechSupply:
    vehicles = ships = mines = 0.0
    for type_id, count in Counter(
        u.type_id for u in self.units.of_type(VEHICLE_TYPES | SHIP_TYPES | MINE_TYPES)
    ).items():
        supply = self.calculate_supply_cost(type_id) * count
        if type_id in VEHICLE_TYPES:
            vehicles += supply
        elif type_id in SHIP_TYPES:
            ships += supply
        else:
            mines += supply
    return MechSupply(vehicles, ships, vehicles + ships + mines)


def is_banking(self: BotAI) -> bool:
    return self.minerals >= UPGRADE_BANK_MINERALS and self.vespene >= UPGRADE_BANK_GAS


MINERAL_BANK_WINDOW = 60.0
MINERAL_BANK_RATE = 150.0
MINERAL_BANK_FLOOR = 300.0


def mineral_income_outpacing_spend(self: BotAI) -> bool:
    samples = self._mineral_bank_samples
    samples.append((self.time, self.minerals))
    while len(samples) > 1 and self.time - samples[0][0] > MINERAL_BANK_WINDOW:
        samples.popleft()
    oldest_time, oldest_minerals = samples[0]
    elapsed = self.time - oldest_time
    if elapsed < MINERAL_BANK_WINDOW * 0.5:
        return False
    rate_per_min = (self.minerals - oldest_minerals) / elapsed * 60.0
    return self.minerals >= MINERAL_BANK_FLOOR and rate_per_min >= MINERAL_BANK_RATE


def next_mech_upgrade(
    self: BotAI, mech: MechSupply, banking: bool, prioritize_armor: bool, skip=()
) -> Optional[UpgradeId]:
    def enough(supply: float, tier: int) -> bool:
        return supply >= MECH_SUPPLY_FOR_TIER[0 if banking else tier]

    def buyable(upgrade: UpgradeId, previous: Optional[UpgradeId]) -> bool:
        return (
            upgrade not in skip
            and self.already_pending_upgrade(upgrade) == 0
            and (previous is None or self.already_pending_upgrade(previous) == 1)
            and self.can_afford(upgrade)
        )

    for tier in range(3):
        wanted = []
        if enough(mech.vehicles, tier):
            wanted.append((VEHICLE_WEAPONS[tier], VEHICLE_WEAPONS[tier - 1] if tier else None))
        if enough(mech.armor, tier):
            wanted.append(
                (VEHICLE_AND_SHIP_ARMOR[tier], VEHICLE_AND_SHIP_ARMOR[tier - 1] if tier else None)
            )
        if prioritize_armor:
            wanted.reverse()
        for upgrade, previous in wanted:
            if buyable(upgrade, previous):
                return upgrade
    for tier in range(3):
        if enough(mech.ships, tier) and buyable(
            SHIP_WEAPONS[tier], SHIP_WEAPONS[tier - 1] if tier else None
        ):
            return SHIP_WEAPONS[tier]
    return None


INFANTRY_UPGRADE_MIN_BASES = 3


def handle_upgrades(self: BotAI):
    prioritize_armor = self.army_advisor.marine_marauder_ratio > 0.7

    bartechs = self.structures(UnitTypeId.BARRACKSTECHLAB).ready.idle
    for tech in bartechs:
        if self.can_afford(UpgradeId.STIMPACK) and self.already_pending_upgrade(UpgradeId.STIMPACK) == 0:
            tech.research(UpgradeId.STIMPACK)
        elif self.can_afford(UpgradeId.SHIELDWALL) and self.already_pending_upgrade(UpgradeId.SHIELDWALL) == 0:
            tech.research(UpgradeId.SHIELDWALL)
        elif self.can_afford(UpgradeId.PUNISHERGRENADES) and self.already_pending_upgrade(UpgradeId.PUNISHERGRENADES) == 0:
            tech.research(UpgradeId.PUNISHERGRENADES)

    sttechs = self.structures(UnitTypeId.STARPORTTECHLAB).ready.idle
    for tech in sttechs:
        if self.can_afford(UpgradeId.BANSHEECLOAK) and self.already_pending_upgrade(UpgradeId.BANSHEECLOAK) == 0:
            tech.research(UpgradeId.BANSHEECLOAK)

    if self.townhalls.amount >= INFANTRY_UPGRADE_MIN_BASES:
        engis = self.structures(UnitTypeId.ENGINEERINGBAY).ready.idle
        infantry_tiers = [
            (UpgradeId.TERRANINFANTRYWEAPONSLEVEL1, UpgradeId.TERRANINFANTRYARMORSLEVEL1),
            (UpgradeId.TERRANINFANTRYWEAPONSLEVEL2, UpgradeId.TERRANINFANTRYARMORSLEVEL2),
            (UpgradeId.TERRANINFANTRYWEAPONSLEVEL3, UpgradeId.TERRANINFANTRYARMORSLEVEL3),
        ]
        for engi in engis:
            for weapon, armor in infantry_tiers:
                first, second = (armor, weapon) if prioritize_armor else (weapon, armor)
                if self.can_afford(first) and self.already_pending_upgrade(first) == 0:
                    engi.research(first)
                    break
                if self.can_afford(second) and self.already_pending_upgrade(second) == 0:
                    engi.research(second)
                    break
            else:
                if (
                    self.can_afford(UpgradeId.TERRANBUILDINGARMOR)
                    and self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL3) == 1
                    and self.already_pending_upgrade(UpgradeId.TERRANBUILDINGARMOR) == 0
                ):
                    engi.research(UpgradeId.TERRANBUILDINGARMOR)
                elif (
                    self.can_afford(UpgradeId.HISECAUTOTRACKING)
                    and self.already_pending_upgrade(UpgradeId.TERRANINFANTRYWEAPONSLEVEL3) == 1
                    and self.already_pending_upgrade(UpgradeId.HISECAUTOTRACKING) == 0
                ):
                    engi.research(UpgradeId.HISECAUTOTRACKING)

    mech = mech_supply(self)
    banking = is_banking(self)
    ordered: Set[UpgradeId] = set()
    for armo in self.structures(UnitTypeId.ARMORY).ready.idle:
        upgrade = next_mech_upgrade(self, mech, banking, prioritize_armor, ordered)
        if upgrade is not None:
            armo.research(upgrade)
            ordered.add(upgrade)


CYCLONE_UPGRADES = (
    (AbilityId.RESEARCH_CYCLONELOCKONDAMAGE, UpgradeId.CYCLONELOCKONDAMAGEUPGRADE),
    (AbilityId.RESEARCH_CYCLONERAPIDFIRELAUNCHERS, UpgradeId.CYCLONERAPIDFIRELAUNCHERS),
    (AbilityId.FACTORYTECHLABRESEARCH_CYCLONERESEARCHHURRICANETHRUSTERS, UpgradeId.HURRICANETHRUSTERS),
    (AbilityId.FACTORYTECHLABRESEARCH_RESEARCHLOCKONRANGEUPGRADE, UpgradeId.CYCLONELOCKONRANGEUPGRADE),
)
CYCLONE_UPGRADE_LOOK_SECONDS = 3.0


async def research_cyclone_upgrade(self: BotAI):
    if self.time < self.cyclone_upgrade_next_look:
        return
    if self.units(UnitTypeId.CYCLONE).amount + self.already_pending(UnitTypeId.CYCLONE) == 0:
        return
    techlabs: Units = self.structures(UnitTypeId.FACTORYTECHLAB).ready.idle
    if techlabs.amount == 0:
        return
    self.cyclone_upgrade_next_look = self.time + CYCLONE_UPGRADE_LOOK_SECONDS
    offered = await self.get_available_abilities(techlabs, ignore_resource_requirements=True)
    for techlab, abilities in zip(techlabs, offered):
        for ability, upgrade in CYCLONE_UPGRADES:
            if ability in abilities and self.already_pending_upgrade(upgrade) == 0 and self.can_afford(upgrade):
                techlab(ability)
                return


def is_supply_critical(self: BotAI) -> bool:
    if self.supply_cap >= 200 or self.supply_used < 14:
        return False

    production_buildings = (
        self.structures(UnitTypeId.BARRACKS).ready.amount +
        self.structures(UnitTypeId.FACTORY).ready.amount +
        self.structures(UnitTypeId.STARPORT).ready.amount
    )

    threshold = 8 + min(production_buildings * 2, 8)
    return self.supply_left < threshold


HALF_OFFSET = Point2((.5, .5))


async def handle_supply(self: BotAI):
    if self.supply_cap >= 200 or (self.worker_rushed and not self.army_advisor.is_wall_closed()):
        return

    if (
        is_supply_critical(self)
        and self.can_afford(UnitTypeId.SUPPLYDEPOT)
        and self.already_pending(UnitTypeId.SUPPLYDEPOT) < 3
        and len(self.build_order) == 0
    ):
        if (
            not self.army_advisor.is_wall_closed()
            and self.mediator.can_place_structure(
                position=self.main_base_ramp.depot_in_middle,
                structure_type=UnitTypeId.SUPPLYDEPOT,
            )
        ):
            await self.build(
                UnitTypeId.SUPPLYDEPOT, near=self.main_base_ramp.depot_in_middle, max_distance=0
            )
            return

        for cc in self.townhalls.ready:
            mfs = cy_closer_than(self.mineral_field, 10, cc.position)
            if len(mfs) == 0:
                continue

            if pos := self.mediator.request_building_placement(
                base_location=cc.position,
                structure_type=UnitTypeId.SUPPLYDEPOT,
                find_alternative=False,
                reserve_placement=False,
            ):
                await self.build(UnitTypeId.SUPPLYDEPOT, near=pos, max_distance=0)
                return

            x = 0
            y = 0
            for i in mfs:
                x += i.position.x
                y += i.position.y
            x = x // len(mfs)
            y = y // len(mfs)
            for i in range(20):
                position = cc.position.towards_with_random_angle(Point2((x, y)), 8, (math.pi / 3))
                position_further = cc.position.towards_with_random_angle(Point2((x, y)), 11, (math.pi / 3))
                position = position.rounded.offset(HALF_OFFSET)
                position_further = position_further.rounded.offset(HALF_OFFSET)
                if self.mediator.can_place_structure(position=position, structure_type=UnitTypeId.SUPPLYDEPOT):
                    await self.build(UnitTypeId.SUPPLYDEPOT, near=position, max_distance=4)
                    return
                if self.mediator.can_place_structure(position=position_further, structure_type=UnitTypeId.SUPPLYDEPOT):
                    await self.build(UnitTypeId.SUPPLYDEPOT, near=position_further, max_distance=4)
                    return


async def handle_command_centers(self: BotAI):
    if self.minerals < 800:
        for oc in self.townhalls(UnitTypeId.ORBITALCOMMAND).filter(lambda x: x.energy >= 50):
            mfs = cy_closer_than(self.mineral_field, 10, oc.position)
            if mfs:
                mf: Unit = max(mfs, key=lambda x: x.mineral_contents)
                oc(AbilityId.CALLDOWNMULE_CALLDOWNMULE, mf)

    if len(self.build_order) == 0:
        bases = sorted(self.townhalls, key=lambda cc: cy_distance_to_squared(cc.position, self.start_location))
        first_three_tags = {cc.tag for cc in bases[:3]}

        for cc in self.townhalls(UnitTypeId.COMMANDCENTER).ready:
            if cc.is_using_ability(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND) or cc.is_using_ability(AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS):
                continue

            if cc.tag in first_three_tags:
                if self.can_afford(UnitTypeId.ORBITALCOMMAND):
                    cc(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND)
                    break
            else:
                if self.can_afford(UnitTypeId.PLANETARYFORTRESS):
                    cc(AbilityId.UPGRADETOPLANETARYFORTRESS_PLANETARYFORTRESS)
                    break

    for cc in self.townhalls.ready:
        if (
            cc.type_id in {UnitTypeId.COMMANDCENTER, UnitTypeId.ORBITALCOMMAND, UnitTypeId.PLANETARYFORTRESS}
            and cc.health < 600
            and self.visible_enemy_units.amount > 0
            and cy_distance_to_squared(cy_closest_to(cc.position, self.visible_enemy_units).position, cc.position) < 64
        ):
            if cc.type_id == UnitTypeId.PLANETARYFORTRESS:
                continue

            if cc.is_using_ability(AbilityId.UPGRADETOORBITAL_ORBITALCOMMAND):
                cc(AbilityId.CANCEL_MORPHORBITAL)
            if cc.is_using_ability(AbilityId.COMMANDCENTERTRAIN_SCV):
                cc(AbilityId.CANCEL_LAST)
            self.lifted_cc_pos[cc.tag] = cc.position
            cc(AbilityId.LIFT)
            continue

        if cc.type_id in {UnitTypeId.COMMANDCENTERFLYING, UnitTypeId.ORBITALCOMMANDFLYING}:
            if cc.health < 600 or (
                self.visible_enemy_units.amount > 0
                and cy_distance_to_squared(cy_closest_to(cc.position, self.visible_enemy_units).position, cc.position) < 64
            ):
                cc.move(Point2(cy_towards(self.start_location, self.game_info.map_center, 6)))
            else:
                expo_pos = self.lifted_cc_pos[cc.tag]
                if cy_distance_to_squared(cc.position, expo_pos) < 1:
                    cc(AbilityId.LAND, expo_pos)
                else:
                    cc.move(expo_pos)


def build_worker(self: BotAI):
    if not self.can_afford(UnitTypeId.SCV) or self.townhalls.amount == 0:
        return
    if self.worker_rushed and (
        (
            self.structures(UnitTypeId.BARRACKS).amount == 0
            or self.structures(UnitTypeId.BARRACKS).first.is_idle
        )
        or not self.army_advisor.is_wall_closed()
    ):
        return

    workers = self.workers.amount
    pending = cy_unit_pending(self, UnitTypeId.SCV)

    if workers + pending >= 68:
        return

    for cc in self.townhalls:
        if cc.type_id in {UnitTypeId.COMMANDCENTER, UnitTypeId.ORBITALCOMMAND}:
            if cc.is_idle:
                cc.train(UnitTypeId.SCV)
                pending += 1
                if workers + pending >= 68:
                    break


async def get_safest_expansion(self: BotAI):
    location: Point2 = await self.get_next_expansion()
    if location is not None:
        enemy_units = self.visible_enemy_units
        enemy_structures = self.enemy_structures
        units_far = (
            enemy_units.amount == 0
            or cy_distance_to_squared(cy_closest_to(location, enemy_units).position, location) > 144
        )
        structures_far = (
            enemy_structures.amount == 0
            or cy_distance_to_squared(cy_closest_to(location, enemy_structures).position, location) > 144
        )
        if units_far and structures_far:
            return location

    already_taken_positions = [pos.position for pos in self.townhalls.not_flying]
    closest_dist_sq = float("inf")
    closest_expansion = None
    for i in self.expansion_locations_list:
        dist_sq = cy_distance_to_squared(i, self.start_location)
        if (
            (location is None or i.position != location.position)
            and dist_sq < closest_dist_sq
            and i.position not in already_taken_positions
        ):
            closest_dist_sq = dist_sq
            closest_expansion = i

    if closest_expansion is not None:
        return closest_expansion
    return location


def closest_ramp_point(self: BotAI, position: Point2, max_distance: float = 15.0) -> Optional[Point2]:
    ramps = getattr(self.game_info, "map_ramps", None)
    if not ramps:
        return None
    closest = min(ramps, key=lambda r: cy_distance_to_squared(r.top_center, position))
    if cy_distance_to_squared(closest.top_center, position) > max_distance * max_distance:
        return None
    return closest.top_center


def get_defend_point(self: BotAI) -> Point2:
    townhalls: Units = self.townhalls
    if townhalls.amount >= 4:
        centroid = Point2(
            (
                sum(t.position.x for t in townhalls) / townhalls.amount,
                sum(t.position.y for t in townhalls) / townhalls.amount,
            )
        )
        base: Unit = cy_closest_to(centroid, townhalls)
    else:
        last_built_tag = next(
            (tag for tag in reversed(self.base_build_order) if townhalls.find_by_tag(tag) is not None),
            None,
        )
        base = (
            townhalls.find_by_tag(last_built_tag)
            if last_built_tag is not None
            else cy_closest_to(self.start_location, townhalls)
        )

    if base.position == self.start_location:
        return self.main_base_ramp.barracks_in_middle

    ramp_point = closest_ramp_point(self, base.position)
    if ramp_point is not None:
        return ramp_point

    enemy_base = self.enemy_start_locations[0]
    direction = (enemy_base - base.position).normalized
    distanceModifier = 10
    if townhalls.amount >= 4:
        distanceModifier = 20
    elif townhalls.amount >= 3:
        distanceModifier = 15
    return base.position + direction * distanceModifier


def get_rally_point(self: BotAI) -> Point2:
    if self.townhalls.amount <= 1 and not self.army_advisor.is_wall_closed():
        return self.start_location
    return get_defend_point(self)


def update_rally_points(self: BotAI):
    rally: Point2 = get_rally_point(self)
    if self.rally_point is not None and rally == self.rally_point:
        return
    self.rally_point = rally
    for structure in self.structures.of_type(
        {UnitTypeId.BARRACKS, UnitTypeId.FACTORY, UnitTypeId.STARPORT}
    ).ready:
        structure(AbilityId.SMART, rally)