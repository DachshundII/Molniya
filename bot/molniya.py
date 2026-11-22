from collections import deque
from itertools import chain

from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.ability_id import AbilityId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from ares import AresBot
from ares.consts import UNIT_TYPES_WITH_NO_ROLE

from cython_extensions import enable_safe_mode

from bot.economy.addons import AddonManager
from bot.economy.build_order import early_build_order
from bot.economy.macro import macro
from bot.economy.production import produce
from bot.economy.speedmining import split_workers, mine
from bot.economy.worker_micro import worker_micro
from bot.economy.worker_rush_defense import worker_rush_defense

from bot.strategy.army_composition_advisor import ArmyCompositionAdvisor
from bot.strategy.worker_scouting import scout

from bot.tactics.manager import ArmyManager
from bot.utils.macro_utils import (
    build_worker,
    handle_depot_status,
    handle_upgrades,
    research_cyclone_upgrade,
    handle_supply,
    handle_command_centers,
    get_rally_point,
)


class Molniya(AresBot):

    army: ArmyManager

    def __init__(self):
        enable_safe_mode(False)

        self.unit_command_uses_self_do = False
        self.distance_calculation_method = 2
        self.game_step: int = 2
        self.build_starport_techlab_first = True
        self.worker_rushed = False
        self.worker_rush_clear_since = None
        self.repair_jobs = {}
        self.repair_walks = {}
        self.repair_backoff = {}
        self.turret_backoff = {}
        self.worker_assigned_to_follow = {}
        self.cyclone_upgrade_next_look = 0.0
        self.oracle_fleeing = set()
        self.threat_fleeing = set()
        self.worker_assigned_to_defend = {}
        self.worker_assigned_to_resume_building = {}
        self.worker_assigned_to_expand = {}
        self.townhall_saturations = {}
        self.refineries_age = {}
        self.lifted_cc_pos = {}
        self.produce_from_starports = True
        self.produce_from_factories = True
        self.produce_from_barracks = True
        self.factory_prioritized = False
        self.rally_point = None
        self.base_build_order = []
        self.addons = AddonManager(self)
        self.scout_worker_tag = None
        self.enemy_base_scouted = False
        self.scout_attempted = False
        self.build_order_critical_worker = None
        self.gas_bank_high = False
        self._mineral_bank_samples = deque()

        self.made_banshee = False
        self.made_raven = False
        self.move_to_depot = -1

        self.build_order = [
            UnitTypeId.SUPPLYDEPOT,
            UnitTypeId.BARRACKS,
            UnitTypeId.REFINERY,
            UnitTypeId.ORBITALCOMMAND,
            UnitTypeId.COMMANDCENTER,
            UnitTypeId.SUPPLYDEPOT,
            UnitTypeId.FACTORY,
        ]

        self.army = None
        self._visible_enemies_loop = -1
        self._visible_enemies = None

        UNIT_TYPES_WITH_NO_ROLE.add(UnitTypeId.SCV)

        super().__init__()

    @property
    def visible_enemy_units(self) -> Units:
        loop = self.state.game_loop
        if self._visible_enemies_loop != loop or self._visible_enemies is None:
            self._visible_enemies = self.enemy_units.filter(lambda u: not u.is_memory)
            self._visible_enemies_loop = loop
        return self._visible_enemies

    def is_unit_position_safe(self, unit: Unit) -> bool:
        grid = self.mediator.get_air_grid if unit.is_flying else self.mediator.get_ground_grid
        return self.mediator.is_position_safe(grid=grid, position=unit.position)

    async def on_before_start(self) -> None:
        await super().on_before_start()
        self.client.game_step = self.game_step
        self.client.raw_affects_selection = True
        top_right = Point2((self.game_info.playable_area.right, self.game_info.playable_area.top))
        bottom_right = Point2((self.game_info.playable_area.right, self.game_info.playable_area.y))
        bottom_left = Point2((self.game_info.playable_area.x, self.game_info.playable_area.y))
        top_left = Point2((self.game_info.playable_area.x, self.game_info.playable_area.top))
        self.map_corners = [top_right, bottom_right, bottom_left, top_left]

    async def on_building_construction_started(self, unit: Unit):
        await super().on_building_construction_started(unit)
        if unit.type_id in {UnitTypeId.BARRACKS, UnitTypeId.FACTORY, UnitTypeId.STARPORT}:
            unit(AbilityId.SMART, get_rally_point(self))
        self.register_base(unit)

    async def on_building_construction_complete(self, unit: Unit):
        await super().on_building_construction_complete(unit)
        self.register_base(unit)

    def register_base(self, unit: Unit):
        if unit.type_id == UnitTypeId.COMMANDCENTER and unit.tag not in self.base_build_order:
            self.base_build_order.append(unit.tag)

    async def on_start(self) -> None:
        await super().on_start()
        self.build_order_runner.set_build_completed()
        self.client.game_step = self.game_step
        self.army_advisor = ArmyCompositionAdvisor(self)
        self.army_advisor.provide_advices_startup()
        self.speedmining_positions = self.mediator.get_mineral_target_dict
        split_workers(self)
        self.army = ArmyManager(self)

    async def on_unit_destroyed(self, unit_tag: int):
        await super().on_unit_destroyed(unit_tag)
        self.army_advisor.remove_unit(unit_tag)
        self.army.on_unit_destroyed(unit_tag)

    async def on_step(self, iteration: int):
        if self.townhalls.amount == 0 or self.supply_used == 0:
            await self.client.leave()
            return

        await super().on_step(iteration)

        self.army_advisor.provide_advices()
        self.resource_by_tag = {unit.tag: unit for unit in chain(self.mineral_field, self.gas_buildings)}

        worker_rush_defense(self)
        await scout(self)
        mine(self, iteration)

        handle_depot_status(self)
        await handle_command_centers(self)
        await handle_supply(self)

        if not self.worker_rushed:
            await early_build_order(self)

        await macro(self)
        build_worker(self)
        await self.addons.update()
        produce(self)
        handle_upgrades(self)
        await research_cyclone_upgrade(self)

        worker_micro(self)
        await self.army.update()