from __future__ import annotations

import asyncio
import copy
import ctypes
import enum
import functools
import inspect
import logging
import multiprocessing
import os.path
import re
import sys
import tempfile
import typing
import queue
import zipfile
import io
import random
import concurrent.futures
import time
import uuid
from pathlib import Path

# CommonClient import first to trigger ModuleUpdater
from CommonClient import CommonContext, server_loop, ClientCommandProcessor, gui_enabled, get_base_parser
from Utils import init_logging, is_windows, async_start
from .item import item_names
from .item.item_groups import item_name_groups, unlisted_item_name_groups
from . import options
from .options import (
    MissionOrder, KerriganPrimalStatus, kerrigan_unit_available, KerriganPresence, EnableMorphling,
    GameSpeed, GenericUpgradeItems, GenericUpgradeResearch, ColorChoice, GenericUpgradeMissions,
    LocationInclusion, ExtraLocations, MasteryLocations, SpeedrunLocations, PreventativeLocations, ChallengeLocations,
    VanillaLocations,
    DisableForcedCamera, SkipCutscenes, GrantStoryTech, GrantStoryLevels, TakeOverAIAllies, RequiredTactics,
    SpearOfAdunPresence, SpearOfAdunPresentInNoBuild, SpearOfAdunAutonomouslyCastAbilityPresence,
    SpearOfAdunAutonomouslyCastPresentInNoBuild, EnableVoidTrade
)
from .mission_order.structs import CampaignSlotData, LayoutSlotData, MissionSlotData, MissionEntryRules
from .mission_order.entry_rules import SubRuleRuleData, CountMissionsRuleData
from .mission_tables import MissionFlag
from .transfer_data import normalized_unit_types
from . import SC2World


if __name__ == "__main__":
    init_logging("SC2Client", exception_logger="Client")

logger = logging.getLogger("Client")
sc2_logger = logging.getLogger("Starcraft2")

import nest_asyncio
from worlds._sc2common import bot
from worlds._sc2common.bot.data import Race
from worlds._sc2common.bot.main import run_game
from worlds._sc2common.bot.player import Bot
from worlds.sc2.item.item_tables import (
    lookup_id_to_name, get_full_item_list, ItemData,
    race_to_item_type, ZergItemType, ProtossItemType, upgrade_bundles,
    WEAPON_ARMOR_UPGRADE_MAX_LEVEL,
)
from .locations import SC2WOL_LOC_ID_OFFSET, LocationType, LocationFlag, SC2HOTS_LOC_ID_OFFSET
from .mission_tables import (
    lookup_id_to_mission, SC2Campaign, MissionInfo,
    lookup_id_to_campaign, SC2Mission, campaign_mission_table, SC2Race
)

import colorama
from .options import Option, upgrade_included_names
from NetUtils import ClientStatus, NetworkItem, JSONtoTextParser, JSONMessagePart, add_json_item, add_json_location, add_json_text, JSONTypes
from MultiServer import mark_raw

pool = concurrent.futures.ThreadPoolExecutor(1)
loop = asyncio.get_event_loop_policy().new_event_loop()
nest_asyncio.apply(loop)
MAX_BONUS: int = 28
VICTORY_MODULO: int = 100

# GitHub repo where the Map/mod data is hosted for /download_data command
DATA_REPO_OWNER = "Ziktofel"
DATA_REPO_NAME = "Archipelago-SC2-data"
DATA_API_VERSION = "API4"

# Bot controller
CONTROLLER_HEALTH: int = 38281
CONTROLLER2_HEALTH: int = 38282

# Void Trade
TRADE_UNIT = "AP_TradeStructure" # ID of the unit
TRADE_SEND_BUTTON = "AP_TradeStructureDummySend" # ID of the button
TRADE_RECEIVE_1_BUTTON = "AP_TradeStructureDummyReceive" # ID of the button
TRADE_RECEIVE_5_BUTTON = "AP_TradeStructureDummyReceive5" # ID of the button
TRADE_DATASTORAGE_TEAM = "SC2_VoidTrade_" # + Team
TRADE_DATASTORAGE_SLOT = "slot_" # + Slot
TRADE_DATASTORAGE_LOCK = "_lock"
TRADE_LOCK_TIME = 5000 # Time in ms that the DataStorage may be considered safe to edit
TRADE_LOCK_WAIT_LIMIT = 540000 / 1.4 # Time in ms that the client may spend trying to get a lock (540000 = 9 minutes, 1.4 is 'faster' game speed's time scale)

# Games
STARCRAFT2 = "Starcraft 2"
STARCRAFT2_WOL = "Starcraft 2 Wings of Liberty"


# Data version file path.
# This file is used to tell if the downloaded data are outdated
# Associated with /download_data command
def get_metadata_file() -> str:
    return os.environ["SC2PATH"] + os.sep + "ArchipelagoSC2Metadata.txt"


def _remap_color_option(slot_data_version: int, color: int) -> int:
    """Remap colour options for backwards compatibility with older slot data"""
    if slot_data_version < 4 and color == ColorChoice.option_mengsk:
        return ColorChoice.option_default
    return color


class ConfigurableOptionType(enum.Enum):
    INTEGER = enum.auto()
    ENUM = enum.auto()

class ConfigurableOptionInfo(typing.NamedTuple):
    name: str
    variable_name: str
    option_class: typing.Type[Option]
    option_type: ConfigurableOptionType = ConfigurableOptionType.ENUM
    can_break_logic: bool = False


class ColouredMessage:
    def __init__(self, text: str = '', *, keep_markup: bool = False) -> None:
        self.parts: typing.List[dict] = []
        if text:
            self(text, keep_markup=keep_markup)
    def __call__(self, text: str, *, keep_markup: bool = False) -> 'ColouredMessage':
        add_json_text(self.parts, text, keep_markup=keep_markup)
        return self
    def coloured(self, text: str, colour: str, *, keep_markup: bool = False) -> 'ColouredMessage':
        add_json_text(self.parts, text, type="color", color=colour, keep_markup=keep_markup)
        return self
    def location(self, location_id: int, player_id: int) -> 'ColouredMessage':
        add_json_location(self.parts, location_id, player_id)
        return self
    def item(self, item_id: int, player_id: int, flags: int = 0) -> 'ColouredMessage':
        add_json_item(self.parts, item_id, player_id, flags)
        return self
    def player(self, player_id: int) -> 'ColouredMessage':
        add_json_text(self.parts, str(player_id), type=JSONTypes.player_id)
        return self
    def send(self, ctx: SC2Context) -> None:
        ctx.on_print_json({"data": self.parts, "cmd": "PrintJSON"})


class StarcraftClientProcessor(ClientCommandProcessor):
    ctx: SC2Context

    def formatted_print(self, text: str) -> None:
        """Prints with kivy formatting to the GUI, and also prints to command-line and to all logs"""
        # Note(mm): Bold/underline can help readability, but unfortunately the CommonClient does not filter bold tags from command-line output.
        # Regardless, using `on_print_json` to get formatted text in the GUI and output in the command-line and in the logs,
        # without having to branch code from CommonClient
        self.ctx.on_print_json({"data": [{"text": text, "keep_markup": True}]})

    def _cmd_difficulty(self, difficulty: str = "") -> bool:
        """Overrides the current difficulty set for the world.  Takes the argument casual, normal, hard, or brutal"""
        arguments = difficulty.split()
        num_arguments = len(arguments)

        if num_arguments > 0:
            difficulty_choice = arguments[0].lower()
            if difficulty_choice == "casual":
                self.ctx.difficulty_override = 0
            elif difficulty_choice == "normal":
                self.ctx.difficulty_override = 1
            elif difficulty_choice == "hard":
                self.ctx.difficulty_override = 2
            elif difficulty_choice == "brutal":
                self.ctx.difficulty_override = 3
            else:
                self.output("Unable to parse difficulty '" + arguments[0] + "'")
                return False

            self.output("Difficulty set to " + arguments[0])
            return True

        else:
            if self.ctx.difficulty == -1:
                self.output("Please connect to a seed before checking difficulty.")
            else:
                current_difficulty = self.ctx.difficulty
                if self.ctx.difficulty_override >= 0:
                    current_difficulty = self.ctx.difficulty_override
                self.output("Current difficulty: " + ["Casual", "Normal", "Hard", "Brutal"][current_difficulty])
            self.output("To change the difficulty, add the name of the difficulty after the command.")
            return False


    def _cmd_game_speed(self, game_speed: str = "") -> bool:
        """Overrides the current game speed for the world.
         Takes the arguments default, slower, slow, normal, fast, faster"""
        arguments = game_speed.split()
        num_arguments = len(arguments)

        if num_arguments > 0:
            speed_choice = arguments[0].lower()
            if speed_choice == "default":
                self.ctx.game_speed_override = 0
            elif speed_choice == "slower":
                self.ctx.game_speed_override = 1
            elif speed_choice == "slow":
                self.ctx.game_speed_override = 2
            elif speed_choice == "normal":
                self.ctx.game_speed_override = 3
            elif speed_choice == "fast":
                self.ctx.game_speed_override = 4
            elif speed_choice == "faster":
                self.ctx.game_speed_override = 5
            else:
                self.output("Unable to parse game speed '" + arguments[0] + "'")
                return False

            self.output("Game speed set to " + arguments[0])
            return True

        else:
            if self.ctx.game_speed == -1:
                self.output("Please connect to a seed before checking game speed.")
            else:
                current_speed = self.ctx.game_speed
                if self.ctx.game_speed_override >= 0:
                    current_speed = self.ctx.game_speed_override
                self.output("Current game speed: "
                            + ["Default", "Slower", "Slow", "Normal", "Fast", "Faster"][current_speed])
            self.output("To change the game speed, add the name of the speed after the command,"
                        " or Default to select based on difficulty.")
            return False
    
    @mark_raw
    def _cmd_received(self, filter_search: str = "") -> bool:
        """List received items.
        Pass in a parameter to filter the search by partial item name or exact item group.
        Use '/received recent <number>' to list the last 'number' items received (default 20)."""
        if filter_search.casefold().startswith('recent'):
            return self._received_recent(filter_search[len('recent'):].strip())
        # Groups must be matched case-sensitively, so we properly capitalize the search term
        # eg. "Spear of Adun" over "Spear Of Adun" or "spear of adun"
        # This fails a lot of item name matches, but those should be found by partial name match
        group_filter = ''
        for group_name in item_name_groups:
            if group_name in unlisted_item_name_groups:
                continue
            if filter_search.casefold() == group_name.casefold():
                group_filter = group_name
                break

        def item_matches_filter(item_name: str) -> bool:
            # The filter can be an exact group name or a partial item name
            # Partial item name can be matched case-insensitively
            if filter_search.casefold() in item_name.casefold():
                return True
            # The search term should already be formatted as a group name
            if group_filter and item_name in item_name_groups[group_filter]:
                return True
            return False

        items = get_full_item_list()
        categorized_items: typing.Dict[SC2Race, typing.List[int]] = {}
        parent_to_child: typing.Dict[int, typing.List[int]] = {}
        items_received: typing.Dict[int, typing.List[NetworkItem]] = {}
        filter_match_count = 0
        for item in self.ctx.items_received:
            items_received.setdefault(item.item, []).append(item)
        items_received_set = set(items_received)
        for item_data in items.values():
            if item_data.parent_item:
                parent_to_child.setdefault(items[item_data.parent_item].code, []).append(item_data.code)
            else:
                categorized_items.setdefault(item_data.race, []).append(item_data.code)
        for faction in SC2Race:
            has_printed_faction_title = False
            def print_faction_title():
                if not has_printed_faction_title:
                    self.formatted_print(f" [u]{faction.name}[/u] ")
            
            for item_id in categorized_items[faction]:
                item_name = self.ctx.item_names.lookup_in_game(item_id)
                received_child_items = items_received_set.intersection(parent_to_child.get(item_id, []))
                matching_children = [child for child in received_child_items
                                     if item_matches_filter(self.ctx.item_names.lookup_in_game(child))]
                received_items_of_this_type = items_received.get(item_id, [])
                item_is_match = item_matches_filter(item_name)
                if item_is_match or len(matching_children) > 0:
                    # Print found item if it or its children match the filter
                    if item_is_match:
                        filter_match_count += len(received_items_of_this_type)
                    for item in received_items_of_this_type:
                        print_faction_title()
                        has_printed_faction_title = True
                        (ColouredMessage('* ').item(item.item, self.ctx.slot, flags=item.flags)
                            (" from ").location(item.location, item.player)
                            (" by ").player(item.player)
                        ).send(self.ctx)
                
                if received_child_items:
                    # We have this item's children
                    if len(matching_children) == 0:
                        # ...but none of them match the filter
                        continue

                    if not received_items_of_this_type:
                        # We didn't receive the item itself
                        print_faction_title()
                        has_printed_faction_title = True
                        ColouredMessage("- ").coloured(item_name, "black")(" - not obtained").send(self.ctx)
                    
                    for child_item in matching_children:
                        received_items_of_this_type = items_received.get(child_item, [])
                        for item in received_items_of_this_type:
                            filter_match_count += len(received_items_of_this_type)
                            (ColouredMessage('  * ').item(item.item, self.ctx.slot, flags=item.flags)
                                (" from ").location(item.location, item.player)
                                (" by ").player(item.player)
                            ).send(self.ctx)
                    
                    non_matching_children = len(received_child_items) - len(matching_children)
                    if non_matching_children > 0:
                        self.formatted_print(f"  + {non_matching_children} child items that don't match the filter")
        if filter_search == "":
            self.formatted_print(f"[b]Obtained: {len(self.ctx.items_received)} items[/b]")
        else:
            self.formatted_print(f"[b]Filter \"{filter_search}\" found {filter_match_count} out of {len(self.ctx.items_received)} obtained items[/b]")
        return True

    def _received_recent(self, amount: str) -> None:
        try:
            display_amount = int(amount)
        except ValueError:
            display_amount = 20
        display_amount = min(display_amount, len(self.ctx.items_received))
        self.formatted_print(f"Last {display_amount} of {len(self.ctx.items_received)} items received (most recent last):")
        for item in self.ctx.items_received[-display_amount:]:
            (
                ColouredMessage()
                .item(item.item, self.ctx.slot, item.flags)
                (" from ").location(item.location, item.player)
                (" by ").player(item.player)
            ).send(self.ctx)
        return True

    def _cmd_option(self, option_name: str = "", option_value: str = "") -> None:
        """Sets a Starcraft game option that can be changed after generation. Use "/option list" to see all options."""

        LOGIC_WARNING = f"  *Note changing this may result in logically unbeatable games*\n"

        configurable_options = (
            ConfigurableOptionInfo('kerrigan_presence', 'kerrigan_presence', options.KerriganPresence, can_break_logic=True),
            ConfigurableOptionInfo('kerrigan_level_cap', 'kerrigan_total_level_cap', options.KerriganTotalLevelCap, ConfigurableOptionType.INTEGER, can_break_logic=True),
            ConfigurableOptionInfo('kerrigan_mission_level_cap', 'kerrigan_levels_per_mission_completed_cap', options.KerriganLevelsPerMissionCompletedCap, ConfigurableOptionType.INTEGER),
            ConfigurableOptionInfo('kerrigan_levels_per_mission', 'kerrigan_levels_per_mission_completed', options.KerriganLevelsPerMissionCompleted, ConfigurableOptionType.INTEGER),
            ConfigurableOptionInfo('grant_story_levels', 'grant_story_levels', options.GrantStoryLevels, can_break_logic=True),
            ConfigurableOptionInfo('grant_story_tech', 'grant_story_tech', options.GrantStoryTech, can_break_logic=True),
            ConfigurableOptionInfo('control_ally', 'take_over_ai_allies', options.TakeOverAIAllies, can_break_logic=True),
            ConfigurableOptionInfo('soa_presence', 'spear_of_adun_presence', options.SpearOfAdunPresence, can_break_logic=True),
            ConfigurableOptionInfo('soa_in_nobuilds', 'spear_of_adun_present_in_no_build', options.SpearOfAdunPresentInNoBuild, can_break_logic=True),
            # Note(mm): Technically SOA passive presence is in the logic for Amon's Fall if Takeover AI Allies is true,
            # but that's edge case enough I don't think we should warn about it.
            ConfigurableOptionInfo('soa_passive_presence', 'spear_of_adun_autonomously_cast_ability_presence', options.SpearOfAdunAutonomouslyCastAbilityPresence),
            ConfigurableOptionInfo('soa_passives_in_nobuilds', 'spear_of_adun_autonomously_cast_present_in_no_build', options.SpearOfAdunAutonomouslyCastPresentInNoBuild),
            ConfigurableOptionInfo('minerals_per_item', 'minerals_per_item', options.MineralsPerItem, ConfigurableOptionType.INTEGER),
            ConfigurableOptionInfo('gas_per_item', 'vespene_per_item', options.VespenePerItem, ConfigurableOptionType.INTEGER),
            ConfigurableOptionInfo('supply_per_item', 'starting_supply_per_item', options.StartingSupplyPerItem, ConfigurableOptionType.INTEGER),
            ConfigurableOptionInfo('no_forced_camera', 'disable_forced_camera', options.DisableForcedCamera),
            ConfigurableOptionInfo('skip_cutscenes', 'skip_cutscenes', options.SkipCutscenes),
            ConfigurableOptionInfo('enable_morphling', 'enable_morphling', options.EnableMorphling, can_break_logic=True),
        )

        WARNING_COLOUR = "salmon"
        CMD_COLOUR = "slateblue"
        boolean_option_map = {
            'y': 'true', 'yes': 'true', 'n': 'false', 'no': 'false',
        }

        help_message = ColouredMessage(inspect.cleandoc("""
            Options
        --------------------
        """))('\n')
        for option in configurable_options:
            option_help_text = inspect.cleandoc(option.option_class.__doc__ or "No description provided.").split('\n', 1)[0]
            help_message.coloured(option.name, CMD_COLOUR)(": " + " | ".join(option.option_class.options)
                + f" -- {option_help_text}\n")
            if option.can_break_logic:
                help_message.coloured(LOGIC_WARNING, WARNING_COLOUR)
        help_message("--------------------\nEnter an option without arguments to see its current value.\n")

        if not option_name or option_name == 'list' or option_name == 'help':
            help_message.send(self.ctx)
            return
        for option in configurable_options:
            if option_name == option.name:
                option_value = boolean_option_map.get(option_value, option_value)
                if not option_value:
                    pass
                elif option.option_type == ConfigurableOptionType.ENUM and option_value in option.option_class.options:
                    self.ctx.__dict__[option.variable_name] = option.option_class.options[option_value]
                elif option.option_type == ConfigurableOptionType.INTEGER:
                    try:
                        self.ctx.__dict__[option.variable_name] = int(option_value, base=0)
                    except:
                        self.output(f"{option_value} is not a valid integer")
                else:
                    self.output(f"Unknown option value '{option_value}'")
                ColouredMessage(f"{option.name} is '{option.option_class.get_option_name(self.ctx.__dict__[option.variable_name])}'").send(self.ctx)
                break
        else:
            self.output(f"Unknown option '{option_name}'")
            help_message.send(self.ctx)

    def _cmd_color(self, faction: str = "", color: str = "") -> None:
        """Changes the player color for a given faction."""
        player_colors = [
            "White", "Red", "Blue", "Teal",
            "Purple", "Yellow", "Orange", "Green",
            "LightPink", "Violet", "LightGrey", "DarkGreen",
            "Brown", "LightGreen", "DarkGrey", "Pink",
            "Rainbow", "Mengsk", "BrightLime", "Arcane", "Ember", "HotPink",
            "Random", "Default"
        ]
        var_names = {
            'raynor': 'player_color_raynor',
            'kerrigan': 'player_color_zerg',
            'primal': 'player_color_zerg_primal',
            'protoss': 'player_color_protoss',
            'nova': 'player_color_nova',
        }
        faction = faction.lower()
        if not faction:
            for faction_name, key in var_names.items():
                self.output(f"Current player color for {faction_name}: {player_colors[self.ctx.__dict__[key]]}")
            self.output("To change your color, add the faction name and color after the command.")
            self.output("Available factions: " + ', '.join(var_names))
            self.output("Available colors: " + ', '.join(player_colors))
            return
        elif faction not in var_names:
            self.output(f"Unknown faction '{faction}'.")
            self.output("Available factions: " + ', '.join(var_names))
            return
        match_colors = [player_color.lower() for player_color in player_colors]
        if not color:
            self.output(f"Current player color for {faction}: {player_colors[self.ctx.__dict__[var_names[faction]]]}")
            self.output("To change this faction's colors, add the name of the color after the command.")
            self.output("Available colors: " + ', '.join(player_colors))
        else:
            if color.lower() not in match_colors:
                self.output(color + " is not a valid color.  Available colors: " + ', '.join(player_colors))
                return
            if color.lower() == "random":
                color = random.choice(player_colors[:-2])
            self.ctx.__dict__[var_names[faction]] = match_colors.index(color.lower())
            self.ctx.pending_color_update = True
            self.output(f"Color for {faction} set to " + player_colors[self.ctx.__dict__[var_names[faction]]])

    def _cmd_windowed_mode(self, value="") -> None:
        """Controls whether sc2 will launch in Windowed mode. Persists across sessions."""
        if not value:
            sc2_logger.info("Use `/windowed_mode [true|false]` to set the windowed mode")
        elif value.casefold() in ('t', 'true', 'yes', 'y'):
            SC2World.settings.game_windowed_mode = True
            force_settings_save_on_close()
        else:
            SC2World.settings.game_windowed_mode = False
            force_settings_save_on_close()
        sc2_logger.info(f"Windowed mode is: {SC2World.settings.game_windowed_mode}")

    def _cmd_disable_mission_check(self) -> bool:
        """Disables the check to see if a mission is available to play.  Meant for co-op runs where one player can play
        the next mission in a chain the other player is doing."""
        self.ctx.missions_unlocked = True
        sc2_logger.info("Mission check has been disabled")
        return True

    @mark_raw
    def _cmd_set_path(self, path: str = '') -> bool:
        """Manually set the SC2 install directory (if the automatic detection fails)."""
        if path:
            os.environ["SC2PATH"] = path
            is_mod_installed_correctly()
            return True
        else:
            sc2_logger.warning("When using set_path, you must type the path to your SC2 install directory.")
        return False

    def _cmd_download_data(self) -> bool:
        """Download the most recent release of the necessary files for playing SC2 with
        Archipelago. Will overwrite existing files."""
        pool.submit(self._download_data, self.ctx)
        return True

    @staticmethod
    def _download_data(ctx: SC2Context) -> bool:
        if "SC2PATH" not in os.environ:
            check_game_install_path()

        if os.path.exists(get_metadata_file()):
            with open(get_metadata_file(), "r") as f:
                metadata = f.read()
        else:
            metadata = None

        tempzip, metadata = download_latest_release_zip(
            DATA_REPO_OWNER, DATA_REPO_NAME, DATA_API_VERSION, metadata=metadata, force_download=True)

        if tempzip:
            try:
                zipfile.ZipFile(tempzip).extractall(path=os.environ["SC2PATH"])
                sc2_logger.info(f"Download complete. Package installed.")
                if metadata is not None:
                    with open(get_metadata_file(), "w") as f:
                        f.write(metadata)
            finally:
                os.remove(tempzip)
        else:
            sc2_logger.warning("Download aborted/failed. Read the log for more information.")
            return False
        ctx.data_out_of_date = False
        return True


class SC2JSONtoTextParser(JSONtoTextParser):
    def __init__(self, ctx) -> None:
        self.handlers = {
            "ItemSend": self._handle_color,
            "ItemCheat": self._handle_color,
            "Hint": self._handle_color,
        }
        super().__init__(ctx)

    def _handle_color(self, node: JSONMessagePart) -> str:
        codes = node["color"].split(";")
        buffer = "".join(self.color_code(code) for code in codes if code in self.color_codes)
        return buffer + self._handle_text(node) + '</c>'

    def color_code(self, code: str) -> str:
        return '<c val="' + self.color_codes[code] + '">'


class SC2Context(CommonContext):
    command_processor = StarcraftClientProcessor
    game = STARCRAFT2
    items_handling = 0b111

    def __init__(self, *args, **kwargs) -> None:
        super(SC2Context, self).__init__(*args, **kwargs)
        self.raw_text_parser = SC2JSONtoTextParser(self)

        self.data_out_of_date: bool = False
        self.difficulty = -1
        self.game_speed = -1
        self.disable_forced_camera = 0
        self.skip_cutscenes = 0
        self.all_in_choice = 0
        self.mission_order = 0
        self.player_color_raynor = ColorChoice.option_blue
        self.player_color_zerg = ColorChoice.option_orange
        self.player_color_zerg_primal = ColorChoice.option_purple
        self.player_color_protoss = ColorChoice.option_blue
        self.player_color_nova = ColorChoice.option_dark_grey
        self.pending_color_update = False
        self.kerrigan_presence: int = KerriganPresence.default
        self.kerrigan_primal_status = 0
        self.enable_morphling = EnableMorphling.default
        self.custom_mission_order: typing.List[CampaignSlotData] = []
        self.mission_id_to_entry_rules: typing.Dict[int, MissionEntryRules]
        self.final_mission_ids: typing.List[int] = [29]
        self.final_locations: typing.List[int] = []
        self.announcements: queue.Queue = queue.Queue()
        self.sc2_run_task: typing.Optional[asyncio.Task] = None
        self.missions_unlocked: bool = False  # allow launching missions ignoring requirements
        self.generic_upgrade_missions = 0
        self.generic_upgrade_research = 0
        self.generic_upgrade_items = 0
        self.location_inclusions: typing.Dict[LocationType, int] = {}
        self.location_inclusions_by_flag: typing.Dict[LocationFlag, int] = {}
        self.plando_locations: typing.List[str] = []
        self.current_tooltip = None
        self.difficulty_override = -1
        self.game_speed_override = -1
        self.mission_id_to_location_ids: typing.Dict[int, typing.List[int]] = {}
        self.last_bot: typing.Optional[ArchipelagoBot] = None
        self.slot_data_version = 2
        self.required_tactics: int = RequiredTactics.default
        self.grant_story_tech: int = GrantStoryTech.default
        self.grant_story_levels: int = GrantStoryLevels.default
        self.take_over_ai_allies: int = TakeOverAIAllies.default
        self.spear_of_adun_presence = SpearOfAdunPresence.option_not_present
        self.spear_of_adun_present_in_no_build = SpearOfAdunPresentInNoBuild.option_false
        self.spear_of_adun_autonomously_cast_ability_presence = SpearOfAdunAutonomouslyCastAbilityPresence.option_not_present
        self.spear_of_adun_autonomously_cast_present_in_no_build = SpearOfAdunAutonomouslyCastPresentInNoBuild.option_false
        self.minerals_per_item: int = 15  # For backwards compat with games generated pre-0.4.5
        self.vespene_per_item: int =  15  # For backwards compat with games generated pre-0.4.5
        self.starting_supply_per_item: int = 2  # For backwards compat with games generated pre-0.4.5
        self.nova_covert_ops_only = False
        self.kerrigan_levels_per_mission_completed = 0
        self.trade_enabled: int = EnableVoidTrade.default
        self.trade_underway: bool = False
        self.trade_latest_reply: typing.Optional[dict] = None
        self.trade_reply_event = asyncio.Event()
        self.trade_lock_wait: int = 0
        self.trade_lock_start: typing.Optional[int] = None
        self.trade_response: typing.Optional[str] = None

    async def server_auth(self, password_requested: bool = False) -> None:
        self.game = STARCRAFT2
        if password_requested and not self.password:
            await super(SC2Context, self).server_auth(password_requested)
        await self.get_username()
        await self.send_connect()
        if self.ui:
            self.ui.first_check = True

    def is_legacy_game(self):
        return self.game == STARCRAFT2_WOL

    def event_invalid_game(self):
        if self.is_legacy_game():
            self.game = STARCRAFT2
            super().event_invalid_game()
        else:
            self.game = STARCRAFT2_WOL
            async_start(self.send_connect())

    def trade_storage_team(self) -> str:
        return f"{TRADE_DATASTORAGE_TEAM}{self.team}"
    
    def trade_storage_slot(self) -> str:
        return f"{TRADE_DATASTORAGE_SLOT}{self.slot}"

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            # Set up the trade storage
            async_start(self.send_msgs([
                { # We want to know about other clients' Set commands for locking
                    "cmd": "SetNotify",
                    "keys": [self.trade_storage_team()],
                },
                {
                    "cmd": "Set",
                    "key": self.trade_storage_team(),
                    "default": { TRADE_DATASTORAGE_LOCK: 0 },
                    "operations": [{"operation": "default", "value": None}]  # value is ignored
                }
            ]))

            self.difficulty = args["slot_data"]["game_difficulty"]
            self.game_speed = args["slot_data"].get("game_speed", GameSpeed.option_default)
            self.disable_forced_camera = args["slot_data"].get("disable_forced_camera", DisableForcedCamera.default)
            self.skip_cutscenes = args["slot_data"].get("skip_cutscenes", SkipCutscenes.default)
            self.all_in_choice = args["slot_data"]["all_in_map"]
            self.slot_data_version = args["slot_data"].get("version", 2)

            if self.slot_data_version < 4:
                # Maintaining backwards compatibility with older slot data
                slot_req_table: dict = args["slot_data"]["mission_req"]

                first_item = list(slot_req_table.keys())[0]
                if first_item in [str(campaign.id) for campaign in SC2Campaign]:
                    # Multi-campaign
                    mission_req_table = {}
                    for campaign_id in slot_req_table:
                        campaign = lookup_id_to_campaign[int(campaign_id)]
                        mission_req_table[campaign] = {
                            mission: self.parse_mission_info(mission_info)
                            for mission, mission_info in slot_req_table[campaign_id].items()
                        }
                else:
                    # Old format
                    mission_req_table = {SC2Campaign.GLOBAL: {
                            mission: self.parse_mission_info(mission_info)
                            for mission, mission_info in slot_req_table.items()
                        }
                    }
                
                self.custom_mission_order = self.parse_mission_req_table(mission_req_table)
                
            if self.slot_data_version >= 4:
                self.custom_mission_order = [
                    CampaignSlotData(
                        **{field:value for field, value in campaign_data.items() if field not in ["layouts", "entry_rule"]},
                        entry_rule = SubRuleRuleData.parse_from_dict(campaign_data["entry_rule"]),
                        layouts = [
                            LayoutSlotData(
                                **{field:value for field, value in layout_data.items() if field not in ["missions", "entry_rule"]},
                                entry_rule = SubRuleRuleData.parse_from_dict(layout_data["entry_rule"]),
                                missions = [
                                    [
                                        MissionSlotData(
                                            **{field:value for field, value in mission_data.items() if field != "entry_rule"},
                                            entry_rule = SubRuleRuleData.parse_from_dict(mission_data["entry_rule"])
                                        ) for mission_data in column
                                    ] for column in layout_data["missions"]
                                ]
                            ) for layout_data in campaign_data["layouts"]
                        ]
                    ) for campaign_data in args["slot_data"]["custom_mission_order"]
                ]
            self.mission_id_to_entry_rules = {
                mission.mission_id: MissionEntryRules(mission.entry_rule, layout.entry_rule, campaign.entry_rule)
                for campaign in self.custom_mission_order for layout in campaign.layouts
                for column in layout.missions for mission in column
            }
                
            self.mission_order = args["slot_data"].get("mission_order", MissionOrder.option_vanilla)
            if self.slot_data_version < 4:
                self.final_mission_ids = [args["slot_data"].get("final_mission", SC2Mission.ALL_IN.id)]
            else:
                self.final_mission_ids = args["slot_data"].get("final_mission_ids", [SC2Mission.ALL_IN.id])
                self.final_locations = [get_location_id(mission_id, 0) for mission_id in self.final_mission_ids]

            self.player_color_raynor = _remap_color_option(
                self.slot_data_version,
                args["slot_data"].get("player_color_terran_raynor", ColorChoice.option_blue)
            )
            self.player_color_zerg = _remap_color_option(
                self.slot_data_version,
                args["slot_data"].get("player_color_zerg", ColorChoice.option_orange)
            )
            self.player_color_zerg_primal = _remap_color_option(
                self.slot_data_version,
                args["slot_data"].get("player_color_zerg_primal", ColorChoice.option_purple)
            )
            self.player_color_protoss = _remap_color_option(
                self.slot_data_version,
                args["slot_data"].get("player_color_protoss", ColorChoice.option_blue)
            )
            self.player_color_nova = _remap_color_option(
                self.slot_data_version,
                args["slot_data"].get("player_color_nova", ColorChoice.option_dark_grey)
            )
            self.generic_upgrade_missions = args["slot_data"].get("generic_upgrade_missions", GenericUpgradeMissions.default)
            self.generic_upgrade_items = args["slot_data"].get("generic_upgrade_items", GenericUpgradeItems.option_individual_items)
            self.generic_upgrade_research = args["slot_data"].get("generic_upgrade_research", GenericUpgradeResearch.option_vanilla)
            self.kerrigan_presence = args["slot_data"].get("kerrigan_presence", KerriganPresence.option_vanilla)
            self.kerrigan_primal_status = args["slot_data"].get("kerrigan_primal_status", KerriganPrimalStatus.option_vanilla)
            self.kerrigan_levels_per_mission_completed = args["slot_data"].get("kerrigan_levels_per_mission_completed", 0)
            self.kerrigan_levels_per_mission_completed_cap = args["slot_data"].get("kerrigan_levels_per_mission_completed_cap", -1)
            self.kerrigan_total_level_cap = args["slot_data"].get("kerrigan_total_level_cap", -1)
            self.enable_morphling = args["slot_data"].get("enable_morphling", EnableMorphling.option_false)
            self.grant_story_tech = args["slot_data"].get("grant_story_tech", GrantStoryTech.option_false)
            self.grant_story_levels = args["slot_data"].get("grant_story_levels", GrantStoryLevels.option_additive)
            self.required_tactics = args["slot_data"].get("required_tactics", RequiredTactics.option_standard)
            self.take_over_ai_allies = args["slot_data"].get("take_over_ai_allies", TakeOverAIAllies.option_false)
            self.spear_of_adun_presence = args["slot_data"].get("spear_of_adun_presence", SpearOfAdunPresence.option_not_present)
            self.spear_of_adun_present_in_no_build = args["slot_data"].get("spear_of_adun_present_in_no_build", SpearOfAdunPresentInNoBuild.option_false)
            self.spear_of_adun_autonomously_cast_ability_presence = args["slot_data"].get("spear_of_adun_autonomously_cast_ability_presence", SpearOfAdunAutonomouslyCastAbilityPresence.option_not_present)
            self.spear_of_adun_autonomously_cast_present_in_no_build = args["slot_data"].get("spear_of_adun_autonomously_cast_present_in_no_build", SpearOfAdunAutonomouslyCastPresentInNoBuild.option_false)
            self.minerals_per_item = args["slot_data"].get("minerals_per_item", 15)
            self.vespene_per_item = args["slot_data"].get("vespene_per_item", 15)
            self.starting_supply_per_item = args["slot_data"].get("starting_supply_per_item", 2)
            self.nova_covert_ops_only = args["slot_data"].get("nova_covert_ops_only", False)
            self.trade_enabled = args["slot_data"].get("enable_void_trade", EnableVoidTrade.option_false)

            if self.required_tactics == RequiredTactics.option_no_logic:
                # Locking Grant Story Tech/Levels if no logic
                self.grant_story_tech = GrantStoryTech.option_true
                self.grant_story_levels = GrantStoryLevels.option_minimum

            self.location_inclusions = {
                LocationType.VICTORY: LocationInclusion.option_enabled, # Victory checks are always enabled
                LocationType.VANILLA: args["slot_data"].get("vanilla_locations", VanillaLocations.default),
                LocationType.EXTRA: args["slot_data"].get("extra_locations", ExtraLocations.default),
                LocationType.CHALLENGE: args["slot_data"].get("challenge_locations", ChallengeLocations.default),
                LocationType.MASTERY: args["slot_data"].get("mastery_locations", MasteryLocations.default),
            }
            self.location_inclusions_by_flag = {
                LocationFlag.SPEEDRUN: args["slot_data"].get("speedrun_locations", SpeedrunLocations.default),
                LocationFlag.PREVENTATIVE: args["slot_data"].get("preventative_locations", PreventativeLocations.default),
            }
            self.plando_locations = args["slot_data"].get("plando_locations", [])

            self.build_location_to_mission_mapping()

            # Looks for the required maps and mods for SC2. Runs check_game_install_path.
            maps_present = is_mod_installed_correctly()
            if os.path.exists(get_metadata_file()):
                with open(get_metadata_file(), "r") as f:
                    current_ver = f.read()
                    sc2_logger.debug(f"Current version: {current_ver}")
                if is_mod_update_available(DATA_REPO_OWNER, DATA_REPO_NAME, DATA_API_VERSION, current_ver):
                    (
                        ColouredMessage().coloured("NOTICE: Update for required files found. ", colour="red")
                        ("Run ").coloured("/download_data", colour="slateblue")
                        (" to install.")
                    ).send(self)
                    self.data_out_of_date = True
            elif maps_present:
                (
                    ColouredMessage()
                    .coloured("NOTICE: Your map files may be outdated (version number not found). ", colour="red")
                    ("Run ").coloured("/download_data", colour="slateblue")
                    (" to install.")
                ).send(self)
                self.data_out_of_date = True
            
            ColouredMessage("[b]Check the Launcher tab to start playing.[/b]", keep_markup=True).send(self)
        
        elif cmd == "SetReply":
            # Currently can only be Void Trade reply
            self.trade_latest_reply = args
            self.trade_reply_event.set()

    @staticmethod
    def parse_mission_info(mission_info: dict[str, typing.Any]) -> MissionInfo:
        if mission_info.get("id") is not None:
            mission_info["mission"] = lookup_id_to_mission[mission_info["id"]]
        elif isinstance(mission_info["mission"], int):
            mission_info["mission"] = lookup_id_to_mission[mission_info["mission"]]

        return MissionInfo(
            **{field: value for field, value in mission_info.items() if field in MissionInfo._fields}
        )
    
    @staticmethod
    def parse_mission_req_table(mission_req_table: typing.Dict[SC2Campaign, typing.Dict[typing.Any, MissionInfo]]) -> typing.List[CampaignSlotData]:
        campaigns: typing.List[typing.Tuple[int, CampaignSlotData]] = []
        rolling_rule_id = 0
        for (campaign, campaign_data) in mission_req_table.items():
            if campaign.campaign_name == "Global":
                campaign_name = ""
            else:
                campaign_name = campaign.campaign_name
            
            categories: typing.Dict[str, typing.List[MissionSlotData]] = {}
            for mission in campaign_data.values():
                if mission.category not in categories:
                    categories[mission.category] = []
                mission_id = mission.mission.id
                sub_rules: typing.List[CountMissionsRuleData] = []
                if mission.number:
                    amount = mission.number
                    missions = [
                        mission.mission.id
                        for mission in mission_req_table[campaign].values()
                    ]
                    sub_rules.append(CountMissionsRuleData(missions, amount, [campaign_name]))
                prev_missions: typing.List[int] = []
                if len(mission.required_world) > 0:
                    missions: typing.List[int] = []
                    for connection in mission.required_world:
                        if isinstance(connection, dict):
                            required_campaign = {}
                            for camp, camp_data in mission_req_table.items():
                                if camp.id == connection["campaign"]:
                                    required_campaign = camp_data
                                    break
                            required_mission_id = connection["connect_to"]
                        else:
                            required_campaign = mission_req_table[connection.campaign]
                            required_mission_id = connection.connect_to
                        required_mission = list(required_campaign.values())[required_mission_id - 1]
                        missions.append(required_mission.mission.id)
                        if required_mission.category == mission.category:
                            prev_missions.append(required_mission.mission.id)
                    if mission.or_requirements:
                        amount = 1
                    else:
                        amount = len(missions)
                    sub_rules.append(CountMissionsRuleData(missions, amount, missions))
                entry_rule = SubRuleRuleData(rolling_rule_id, sub_rules, len(sub_rules))
                rolling_rule_id += 1
                categories[mission.category].append(MissionSlotData.legacy(mission_id, prev_missions, entry_rule))

            layouts: typing.List[LayoutSlotData] = []
            for (layout, mission_slots) in categories.items():
                if layout.startswith("_"):
                    layout_name = ""
                else:
                    layout_name = layout
                layouts.append(LayoutSlotData.legacy(layout_name, [mission_slots]))
            campaigns.append((campaign.id, CampaignSlotData.legacy(campaign_name, layouts)))
        return [data for (_, data) in sorted(campaigns)]


    def on_print_json(self, args: dict) -> None:
        # goes to this world
        if "receiving" in args and self.slot_concerns_self(args["receiving"]):
            relevant = True
        # found in this world
        elif "item" in args and self.slot_concerns_self(args["item"].player):
            relevant = True
        # not related
        else:
            relevant = False

        if relevant:
            self.announcements.put(self.raw_text_parser(copy.deepcopy(args["data"])))

        super(SC2Context, self).on_print_json(args)

    def run_gui(self) -> None:
        from .client_gui import start_gui
        start_gui(self)

    async def shutdown(self) -> None:
        await super(SC2Context, self).shutdown()
        if self.last_bot:
            self.last_bot.want_close = True
        if self.sc2_run_task:
            self.sc2_run_task.cancel()

    def play_mission(self, mission_id: int) -> bool:
        if self.missions_unlocked or is_mission_available(self, mission_id):
            if self.sc2_run_task:
                if not self.sc2_run_task.done():
                    sc2_logger.warning("Starcraft 2 Client is still running!")
                self.sc2_run_task.cancel()  # doesn't actually close the game, just stops the python task
            if self.slot is None:
                sc2_logger.warning("Launching Mission without Archipelago authentication, "
                                   "checks will not be registered to server.")
            self.sc2_run_task = asyncio.create_task(starcraft_launch(self, mission_id),
                                                    name="Starcraft 2 Launch")
            return True
        else:
            sc2_logger.info(f"{lookup_id_to_mission[mission_id].mission_name} is not currently unlocked.")
            return False

    def build_location_to_mission_mapping(self) -> None:
        mission_id_to_location_ids: typing.Dict[int, typing.Set[int]] = {
            mission.mission_id: set()
            for campaign in self.custom_mission_order for layout in campaign.layouts
            for column in layout.missions for mission in column
        }

        for loc in self.server_locations:
            offset = SC2WOL_LOC_ID_OFFSET if loc < SC2HOTS_LOC_ID_OFFSET \
                else (SC2HOTS_LOC_ID_OFFSET - SC2Mission.ALL_IN.id * VICTORY_MODULO)
            mission_id, objective = divmod(loc - offset, VICTORY_MODULO)
            mission_id_to_location_ids[mission_id].add(objective)
        self.mission_id_to_location_ids = {mission_id: sorted(objectives) for mission_id, objectives in
                                           mission_id_to_location_ids.items()}

    def locations_for_mission(self, mission: SC2Mission) -> typing.Iterable[int]:
        mission_id: int = mission.id
        objectives = self.mission_id_to_location_ids[mission_id]
        for objective in objectives:
            yield get_location_id(mission_id, objective)
    
    def locations_for_mission_id(self, mission_id: int) -> typing.Iterable[int]:
        objectives = self.mission_id_to_location_ids[mission_id]
        for objective in objectives:
            yield get_location_id(mission_id, objective)

    def uncollected_locations_in_mission(self, mission: SC2Mission) -> typing.Iterable[int]:
        for location_id in self.locations_for_mission(mission):
            if location_id in self.missing_locations:
                yield location_id

    def is_mission_completed(self, mission_id: int) -> bool:
        return get_location_id(mission_id, 0) in self.checked_locations
    
        
    async def trade_acquire_storage(self, keep_trying: bool = False) -> typing.Optional[dict]:
        # This function was largely taken from the Pokemon Emerald client
        """
        Acquires a lock on the Void Trade DataStorage.
        Locking the key means you have exclusive access
        to modifying the value until you unlock it or the key expires (5 seconds).

        If `keep_trying` is `True`, it will keep trying to acquire the lock
        until successful. Otherwise it will return `None` if it fails to
        acquire the lock.
        """
        while not self.exit_event.is_set() and self.last_bot and self.last_bot.game_running:
            lock = int(time.time_ns() / 1000000)

            # Make sure we're not past the waiting limit
            # SC2 needs to be notified within 10 minutes (training time of the dummy units)
            if self.trade_lock_start is not None:
                if self.last_bot.time - self.trade_lock_start >= TRADE_LOCK_WAIT_LIMIT:
                    self.trade_lock_wait = 0
                    self.trade_lock_start = None
                    return None
            elif keep_trying:
                self.trade_lock_start = self.last_bot.time

            message_uuid = str(uuid.uuid4())
            await self.send_msgs([{
                "cmd": "Set",
                "key": self.trade_storage_team(),
                "default": { TRADE_DATASTORAGE_LOCK: 0 },
                "want_reply": True,
                "operations": [{ "operation": "update", "value": { TRADE_DATASTORAGE_LOCK: lock } }],
                "uuid": message_uuid,
            }])

            self.trade_reply_event.clear()
            try:
                await asyncio.wait_for(self.trade_reply_event.wait(), 5)
            except asyncio.TimeoutError:
                if not keep_trying:
                    return None
                continue

            reply = copy.deepcopy(self.trade_latest_reply)

            # Make sure the most recently received update was triggered by our lock attempt
            if reply.get("uuid", None) != message_uuid:
                if not keep_trying:
                    return None
                await asyncio.sleep(TRADE_LOCK_TIME)
                continue

            # Make sure the current value of the lock is what we set it to
            # (I think this should theoretically never run)
            if reply["value"][TRADE_DATASTORAGE_LOCK] != lock:
                if not keep_trying:
                    return None
                await asyncio.sleep(TRADE_LOCK_TIME)
                continue

            # Make sure that the lock value we replaced is at least 5 seconds old
            # If it was unlocked before our change, its value was 0 and it will look decades old
            if lock - reply["original_value"][TRADE_DATASTORAGE_LOCK] < TRADE_LOCK_TIME:
                if not keep_trying:
                    return None
                
                # Multiple clients trying to lock the key may get stuck in a loop of checking the lock
                # by trying to set it, which will extend its expiration. So if we see that the lock was
                # too new when we replaced it, we should wait for increasingly longer periods so that
                # eventually the lock will expire and a client will acquire it.
                self.trade_lock_wait += TRADE_LOCK_TIME
                self.trade_lock_wait += random.randrange(100, 500)

                await asyncio.sleep(self.trade_lock_wait)
                continue

            # We have the lock, reset the waiting period and return
            self.trade_lock_wait = 0
            self.trade_lock_start = None
            return reply
        return None
        

    async def trade_receive(self, amount: int = 1):
        """
        Tries to pop `amount` units out of the trade storage.
        """
        reply = await self.trade_acquire_storage(True)

        if reply is None:
            self.trade_response = "?TradeFail Void Trade failed: Could not communicate with server. Trade cost refunded."
            return None

        # Find available units
        # Ignore units we sent ourselves
        allowed_slots: typing.List[str] = [
            slot for slot in reply["value"]
            if slot != TRADE_DATASTORAGE_LOCK \
                and slot != self.trade_storage_slot()
        ]
        available_units: typing.List[typing.Tuple[str, str]] = []
        available_counts: typing.List[int] = []
        for slot in allowed_slots:
            for (unit, count) in reply["value"][slot].items():
                available_units.append((unit, slot))
                available_counts.append(count)

        # Pick units to receive
        # If there's not enough units in total, just pick as many as possible
        # SC2 should handle the refund
        available = sum(available_counts)
        refunds = 0
        if available < amount:
            refunds = amount - available
            amount = available
        if available == 0:
            # random.sample crashes if counts is an empty list
            units = []
        else:
            units = random.sample(available_units, amount, counts = available_counts)

        # Build response data
        unit_counts: typing.Dict[str, int] = {}
        slots_to_update: typing.Dict[str, typing.Dict[str, int]] = {}
        for (unit, slot) in units:
            unit_counts[unit] = unit_counts.get(unit, 0) + 1
            if slot not in slots_to_update:
                slots_to_update[slot] = copy.deepcopy(reply["value"][slot])
            slots_to_update[slot][unit] -= 1
            # Clean up units that were removed completely
            if slots_to_update[slot][unit] == 0:
                slots_to_update[slot].pop(unit)

        await self.send_msgs([
            {   # Update server storage
                "cmd": "Set",
                "key": self.trade_storage_team(),
                "operations": [{ "operation": "update", "value": slots_to_update }]
            },
            {   # Release the lock
                "cmd": "Set",
                "key": self.trade_storage_team(),
                "operations": [{ "operation": "update", "value": { TRADE_DATASTORAGE_LOCK: 0 } }]
            }
        ])

        # Give units to bot
        self.trade_response = f"?Trade {refunds} " + " ".join(f"{unit} {count}" for (unit, count) in unit_counts.items())


    async def trade_send(self, units: typing.List[str]):
        """
        Tries to upload `units` to the trade DataStorage.
        """
        reply = await self.trade_acquire_storage(True)

        if reply is None:
            self.trade_response = "?TradeFail Void Trade failed: Could not communicate with server. Your units remain."
            return None
        
        # Update the storage with the new units
        data: typing.Dict[str, int] = copy.deepcopy(reply["value"].get(self.trade_storage_slot(), {}))
        for unit in units:
            data[unit] = data.get(unit, 0) + 1
        
        await self.send_msgs([
            {   # Send the updated data
                "cmd": "Set",
                "key": self.trade_storage_team(),
                "operations": [{ "operation": "update", "value": { self.trade_storage_slot(): data } }]
            },
            {   # Release the lock
                "cmd": "Set",
                "key": self.trade_storage_team(),
                "operations": [{ "operation": "update", "value": { TRADE_DATASTORAGE_LOCK: 0 } }]
            }
        ])
        
        # Notify the game
        self.trade_response = "?TradeSuccess Void Trade successful: Units sent!"


class CompatItemHolder(typing.NamedTuple):
    name: str
    quantity: int = 1


async def main():
    multiprocessing.freeze_support()
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Slot Name to connect as.")
    args = parser.parse_args()

    ctx = SC2Context(args.connect, args.password)
    ctx.auth = args.name
    if ctx.server_task is None:
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    await ctx.exit_event.wait()

    await ctx.shutdown()

# These items must be given to the player if the game is generated on older versions
API2_TO_API3_COMPAT_ITEMS: typing.Set[CompatItemHolder] = {
    CompatItemHolder(item_names.PHOTON_CANNON),
    CompatItemHolder(item_names.OBSERVER),
    CompatItemHolder(item_names.WARP_HARMONIZATION),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_GROUND_WEAPON, 3),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_GROUND_ARMOR, 3),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_SHIELDS, 3),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_AIR_WEAPON, 3),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_AIR_ARMOR, 3),
    CompatItemHolder(item_names.PROGRESSIVE_PROTOSS_WEAPON_ARMOR_UPGRADE, 3)
}
API3_TO_API4_COMPAT_ITEMS: typing.Set[CompatItemHolder] = {
    CompatItemHolder(item_name)
    for item_name, item_data in get_full_item_list().items()
    if item_data.type in (ProtossItemType.War_Council, ProtossItemType.War_Council_2)
       and item_name != item_names.DESTROYER_REFORGED_BLOODSHARD_CORE
       and item_name != item_names.OBSERVER_INDUCE_SCOPOPHOBIA
}


def compat_item_to_network_items(compat_item: CompatItemHolder) -> typing.List[NetworkItem]:
    item_id = get_full_item_list()[compat_item.name].code
    network_item = NetworkItem(item_id, 0, 0, 0)
    return compat_item.quantity * [network_item]


def calculate_items(ctx: SC2Context) -> typing.Dict[SC2Race, typing.List[int]]:
    items = ctx.items_received.copy()
    # Items unlocked in earlier generator versions by default (Prophecy defaults, war council, rebalances)
    if ctx.slot_data_version < 3:
        for compat_item in API2_TO_API3_COMPAT_ITEMS:
            items.extend(compat_item_to_network_items(compat_item))
    if ctx.slot_data_version < 4:
        for compat_item in API3_TO_API4_COMPAT_ITEMS:
            items.extend(compat_item_to_network_items(compat_item))

    # API < 4 Orbital Command Count (Deprecated item)
    orbital_command_count: int = 0

    network_item: NetworkItem
    accumulators: typing.Dict[SC2Race, typing.List[int]] = {
        race: [0 for element in item_type_enum_class if element.flag_word >= 0]
        for race, item_type_enum_class in race_to_item_type.items()
    }

    # Protoss Shield grouped item specific logic
    shields_from_ground_upgrade: int = 0
    shields_from_air_upgrade: int = 0

    item_list = get_full_item_list()
    for network_item in items:
        name: str = lookup_id_to_name[network_item.item]
        item_data: ItemData = item_list[name]

        if item_data.type.flag_word < 0:
            continue

        # exists exactly once
        if item_data.quantity == 1:
            accumulators[item_data.race][item_data.type.flag_word] |= 1 << item_data.number

        # exists multiple times
        elif item_data.quantity > 1:
            flaggroup = item_data.type.flag_word

            # Generic upgrades apply only to Weapon / Armor upgrades
            if item_data.number >= 0:
                accumulators[item_data.race][flaggroup] += 1 << item_data.number
            else:
                if name == item_names.PROGRESSIVE_PROTOSS_GROUND_UPGRADE:
                    shields_from_ground_upgrade += 1
                if name == item_names.PROGRESSIVE_PROTOSS_AIR_UPGRADE:
                    shields_from_air_upgrade += 1
                for bundled_number in get_bundle_upgrade_member_numbers(name):
                    accumulators[item_data.race][flaggroup] += 1 << bundled_number

            # Regen bio-steel nerf with API3 - undo for older games
            if ctx.slot_data_version < 3 and name == item_names.PROGRESSIVE_REGENERATIVE_BIO_STEEL:
                current_level = (accumulators[item_data.race][flaggroup] >> item_data.number) % 4
                if current_level == 2:
                    # Switch from level 2 to level 3 for compatibility
                    accumulators[item_data.race][flaggroup] += 1 << item_data.number
        # sum
        else:
            if name == item_names.PROGRESSIVE_ORBITAL_COMMAND:
                orbital_command_count += 1
            elif name == item_names.STARTING_MINERALS:
                accumulators[item_data.race][item_data.type.flag_word] += ctx.minerals_per_item
            elif name == item_names.STARTING_VESPENE:
                accumulators[item_data.race][item_data.type.flag_word] += ctx.vespene_per_item
            elif name == item_names.STARTING_SUPPLY:
                accumulators[item_data.race][item_data.type.flag_word] += ctx.starting_supply_per_item
            else:
                accumulators[item_data.race][item_data.type.flag_word] += item_data.number

    # Fix Shields from generic upgrades by unit class (Maximum of ground/air upgrades)
    if shields_from_ground_upgrade > 0 or shields_from_air_upgrade > 0:
        shield_upgrade_level = max(shields_from_ground_upgrade, shields_from_air_upgrade)
        shield_upgrade_item = item_list[item_names.PROGRESSIVE_PROTOSS_SHIELDS]
        for _ in range(0, shield_upgrade_level):
            accumulators[shield_upgrade_item.race][shield_upgrade_item.type.flag_word] += 1 << shield_upgrade_item.number

    # Deprecated Orbital Command handling (Backwards compatibility):
    if orbital_command_count > 0:
        orbital_command_replacement_items: typing.List[str] = [
            item_names.COMMAND_CENTER_SCANNER_SWEEP,
            item_names.COMMAND_CENTER_MULE,
            item_names.COMMAND_CENTER_EXTRA_SUPPLIES,
            item_names.PLANETARY_FORTRESS_ORBITAL_MODULE
        ]
        replacement_item_ids = [get_full_item_list()[item_name].code for item_name in orbital_command_replacement_items]
        if sum(item_id in replacement_item_ids for item_id in items) > 0:
            logger.warning(inspect.cleandoc("""
                Both old Orbital Command and its replacements are present in the world. Skipping compatibility handling.
            """))
        else:
            # None of replacement items are present
            # L1: MULE and Scanner Sweep
            scanner_sweep_data = get_full_item_list()[item_names.COMMAND_CENTER_SCANNER_SWEEP]
            mule_data = get_full_item_list()[item_names.COMMAND_CENTER_MULE]
            accumulators[scanner_sweep_data.race][scanner_sweep_data.type.flag_word] += 1 << scanner_sweep_data.number
            accumulators[mule_data.race][mule_data.type.flag_word] += 1 << mule_data.number
            if orbital_command_count >= 2:
                # L2 MULE and Scanner Sweep usable even in Planetary Fortress Mode
                planetary_orbital_module_data = get_full_item_list()[item_names.PLANETARY_FORTRESS_ORBITAL_MODULE]
                accumulators[planetary_orbital_module_data.race][planetary_orbital_module_data.type.flag_word] += \
                    1 << planetary_orbital_module_data.number

    # Upgrades from completed missions
    if ctx.generic_upgrade_missions > 0:
        total_missions = sum(len(column) for campaign in ctx.custom_mission_order for layout in campaign.layouts for column in layout.missions)
        num_missions = int((ctx.generic_upgrade_missions / 100) * total_missions)
        completed = len([mission_id for mission_id in ctx.mission_id_to_location_ids if ctx.is_mission_completed(mission_id)])
        upgrade_count = min(completed // num_missions, WEAPON_ARMOR_UPGRADE_MAX_LEVEL) if num_missions > 0 else WEAPON_ARMOR_UPGRADE_MAX_LEVEL

        # Equivalent to "Progressive Weapon/Armor Upgrade" item
        global_upgrades: typing.Set[str] = upgrade_included_names[GenericUpgradeItems.option_bundle_all]
        for global_upgrade in global_upgrades:
            race = get_full_item_list()[global_upgrade].race
            upgrade_flaggroup = race_to_item_type[race]["Upgrade"].flag_word
            for bundled_number in get_bundle_upgrade_member_numbers(global_upgrade):
                accumulators[race][upgrade_flaggroup] += upgrade_count << bundled_number

    return accumulators


def get_bundle_upgrade_member_numbers(bundled_item: str) -> typing.List[int]:
    upgrade_elements: typing.List[str] = upgrade_bundles[bundled_item]
    if bundled_item in (item_names.PROGRESSIVE_PROTOSS_GROUND_UPGRADE, item_names.PROGRESSIVE_PROTOSS_AIR_UPGRADE):
        # Shields are handled as a maximum of those two
        upgrade_elements = [item_name for item_name in upgrade_elements if item_name != item_names.PROGRESSIVE_PROTOSS_SHIELDS]
    return [get_full_item_list()[item_name].number for item_name in upgrade_elements]


def calc_difficulty(difficulty: int):
    if difficulty == 0:
        return 'C'
    elif difficulty == 1:
        return 'N'
    elif difficulty == 2:
        return 'H'
    elif difficulty == 3:
        return 'B'

    return 'X'


def get_kerrigan_level(ctx: SC2Context, items: typing.Dict[SC2Race, typing.List[int]], missions_beaten: int) -> int:
    item_value = items[SC2Race.ZERG][ZergItemType.Level.flag_word]
    mission_value = missions_beaten * ctx.kerrigan_levels_per_mission_completed
    if ctx.kerrigan_levels_per_mission_completed_cap != -1:
        mission_value = min(mission_value, ctx.kerrigan_levels_per_mission_completed_cap)
    total_value = item_value + mission_value
    if ctx.kerrigan_total_level_cap != -1:
        total_value = min(total_value, ctx.kerrigan_total_level_cap)
    return total_value


def calculate_kerrigan_options(ctx: SC2Context) -> int:
    result = 0

    # Bits 0, 1
    # Kerrigan unit available
    if ctx.kerrigan_presence in kerrigan_unit_available:
        result |= 1 << 0

    # Bit 2
    # Kerrigan primal status by map
    if ctx.kerrigan_primal_status == KerriganPrimalStatus.option_vanilla:
        result |= 1 << 2

    return result


def caclulate_soa_options(ctx: SC2Context) -> int:
    result = 0

    # Bits 0, 1
    # SoA Calldowns available
    soa_presence_value = 0
    if ctx.spear_of_adun_presence == SpearOfAdunPresence.option_not_present:
        soa_presence_value = 0
    elif ctx.spear_of_adun_presence == SpearOfAdunPresence.option_lotv_protoss:
        soa_presence_value = 1
    elif ctx.spear_of_adun_presence == SpearOfAdunPresence.option_protoss:
        soa_presence_value = 2
    elif ctx.spear_of_adun_presence == SpearOfAdunPresence.option_everywhere:
        soa_presence_value = 3
    result |= soa_presence_value << 0

    # Bit 2
    # SoA Calldowns for no-builds
    if ctx.spear_of_adun_present_in_no_build == SpearOfAdunPresentInNoBuild.option_true:
        result |= 1 << 2

    # Bits 3,4
    # Autocasts
    soa_autocasts_presence_value = 0
    if ctx.spear_of_adun_autonomously_cast_ability_presence == SpearOfAdunAutonomouslyCastAbilityPresence.option_not_present:
        soa_autocasts_presence_value = 0
    elif ctx.spear_of_adun_autonomously_cast_ability_presence == SpearOfAdunAutonomouslyCastAbilityPresence.option_lotv_protoss:
        soa_autocasts_presence_value = 1
    elif ctx.spear_of_adun_autonomously_cast_ability_presence == SpearOfAdunAutonomouslyCastAbilityPresence.option_protoss:
        soa_autocasts_presence_value = 2
    elif ctx.spear_of_adun_autonomously_cast_ability_presence == SpearOfAdunAutonomouslyCastAbilityPresence.option_everywhere:
        soa_autocasts_presence_value = 3
    # Guardian Shell breaks without SoA on version 4+, but can be generated without SoA on version 3
    if ctx.slot_data_version < 4 and soa_autocasts_presence_value < 2:
        soa_autocasts_presence_value = 2
    result |= soa_autocasts_presence_value << 3

    # Bit 5
    # Autocasts in no-builds
    if ctx.spear_of_adun_autonomously_cast_present_in_no_build == SpearOfAdunAutonomouslyCastPresentInNoBuild.option_true:
        result |= 1 << 5

    return result

def kerrigan_primal(ctx: SC2Context, kerrigan_level: int) -> bool:
    if ctx.kerrigan_primal_status == KerriganPrimalStatus.option_always_zerg:
        return True
    elif ctx.kerrigan_primal_status == KerriganPrimalStatus.option_always_human:
        return False
    elif ctx.kerrigan_primal_status == KerriganPrimalStatus.option_level_35:
        return kerrigan_level >= 35
    elif ctx.kerrigan_primal_status == KerriganPrimalStatus.option_half_completion:
        total_missions = len(ctx.mission_id_to_location_ids)
        completed = sum(ctx.is_mission_completed(mission_id)
                         for mission_id in ctx.mission_id_to_location_ids)
        return completed >= (total_missions / 2)
    elif ctx.kerrigan_primal_status == KerriganPrimalStatus.option_item:
        codes = [item.item for item in ctx.items_received]
        return get_full_item_list()[item_names.KERRIGAN_PRIMAL_FORM].code in codes
    return False


def get_mission_variant(mission_id: int) -> int:
    mission_flags = lookup_id_to_mission[mission_id].flags
    if MissionFlag.RaceSwap not in mission_flags:
        return 0
    if MissionFlag.Terran in mission_flags:
        return 1
    elif MissionFlag.Zerg in mission_flags:
        return 2
    elif MissionFlag.Protoss in mission_flags:
        return 3
    return 0


async def starcraft_launch(ctx: SC2Context, mission_id: int):
    sc2_logger.info(f"Launching {lookup_id_to_mission[mission_id].mission_name}. If game does not launch check log file for errors.")

    with DllDirectory(None):
        run_game(
            bot.maps.get(lookup_id_to_mission[mission_id].map_file),
            [Bot(Race.Terran, ArchipelagoBot(ctx, mission_id), name="Archipelago", fullscreen=not SC2World.settings.game_windowed_mode)],
            realtime=True,
        )


class ArchipelagoBot(bot.bot_ai.BotAI):
    __slots__ = [
        'game_running',
        'mission_completed',
        'boni',
        'setup_done',
        'ctx',
        'mission_id',
        'want_close',
        'can_read_game',
        'last_received_update',
        'last_trade_cargo',
        'last_supply_used'
    ]
    ctx: SC2Context

    def __init__(self, ctx: SC2Context, mission_id: int):
        self.game_running = False
        self.mission_completed = False
        self.want_close = False
        self.can_read_game = False
        self.last_received_update: int = 0
        self.last_trade_cargo: set = set()
        self.last_supply_used: int = 0
        self.setup_done = False
        self.ctx = ctx
        self.ctx.last_bot = self
        self.mission_id = mission_id
        self.boni = [False for _ in range(MAX_BONUS)]

        super(ArchipelagoBot, self).__init__()

    async def on_step(self, iteration: int):
        if self.want_close:
            self.want_close = False
            await self._client.leave()
            return
        game_state = 0
        if not self.setup_done:
            self.setup_done = True
            start_items = calculate_items(self.ctx)
            missions_beaten = self.missions_beaten_count()
            kerrigan_level = get_kerrigan_level(self.ctx, start_items, missions_beaten)
            kerrigan_options = calculate_kerrigan_options(self.ctx)
            soa_options = caclulate_soa_options(self.ctx)
            mission_variant = get_mission_variant(self.mission_id)  # 0/1/2/3 for unchanged/Terran/Zerg/Protoss
            uncollected_objectives: typing.List[int] = self.get_uncollected_objectives()
            if self.ctx.difficulty_override >= 0:
                difficulty = calc_difficulty(self.ctx.difficulty_override)
            else:
                difficulty = calc_difficulty(self.ctx.difficulty)
            if self.ctx.game_speed_override >= 0:
                game_speed = self.ctx.game_speed_override
            else:
                game_speed = self.ctx.game_speed
            await self.chat_send(
                "?SetOptions"
                f" {difficulty}"
                f" {self.ctx.generic_upgrade_research}"
                f" {self.ctx.all_in_choice}"
                f" {game_speed}"
                f" {self.ctx.disable_forced_camera}"
                f" {self.ctx.skip_cutscenes}"
                f" {kerrigan_options}"
                f" {self.ctx.grant_story_tech}"
                f" {self.ctx.take_over_ai_allies}"
                f" {soa_options}"
                f" {self.ctx.mission_order}"
                f" {int(self.ctx.nova_covert_ops_only)}"
                f" {self.ctx.grant_story_levels}"
                f" {self.ctx.enable_morphling}"
                f" {mission_variant}"
                f" {self.ctx.trade_enabled}"
            )
            await self.chat_send("?GiveResources {} {} {}".format(
                start_items[SC2Race.ANY][0],
                start_items[SC2Race.ANY][1],
                start_items[SC2Race.ANY][2]
            ))
            await self.updateTerranTech(start_items)
            await self.updateZergTech(start_items, kerrigan_level)
            await self.updateProtossTech(start_items)
            await self.updateColors()
            if uncollected_objectives:
                await self.chat_send("?UncollectedLocations {}".format(
                    functools.reduce(lambda a, b: a + " " + b, [str(x) for x in uncollected_objectives])
                ))
            await self.chat_send("?LoadFinished")
            self.last_received_update = len(self.ctx.items_received)

        else:
            if self.ctx.pending_color_update:
                await self.updateColors()

            if not self.ctx.announcements.empty():
                message = self.ctx.announcements.get(timeout=1)
                await self.chat_send("?SendMessage " + message)
                self.ctx.announcements.task_done()

            # Archipelago reads the health
            controller1_state = 0
            controller2_state = 0
            for unit in self.all_own_units():
                if unit.health_max == CONTROLLER_HEALTH:
                    controller1_state = int(CONTROLLER_HEALTH - unit.health)
                    self.can_read_game = True
                elif unit.health_max == CONTROLLER2_HEALTH:
                    controller2_state = int(CONTROLLER2_HEALTH - unit.health)
                    self.can_read_game = True
                elif unit.name == TRADE_UNIT:
                    # Handle Void Trade requests
                    # Check for orders (for buildings this is usually research or training)
                    if not unit.is_idle and not self.ctx.trade_underway:
                        button = unit.orders[0].ability.button_name
                        if button == TRADE_SEND_BUTTON and len(self.last_trade_cargo) > 0:
                            units_to_send: typing.List[str] = []
                            non_ap_units: typing.Set[str] = set()
                            for passenger in self.last_trade_cargo:
                                # Alternatively passenger._type_data.name but passenger.name seems to always match
                                unit_name = passenger.name
                                if unit_name.startswith("AP_"):
                                    units_to_send.append(normalized_unit_types.get(unit_name, unit_name))
                                else:
                                    non_ap_units.add(unit_name)
                            if len(non_ap_units) > 0:
                                sc2_logger.info(f"Void Trade tried to send non-AP units: {', '.join(non_ap_units)}")
                                self.ctx.trade_response = "?TradeFail Void Trade rejected: Trade contains invalid units."
                                self.ctx.trade_underway = True
                            else:
                                self.ctx.trade_response = None
                                self.ctx.trade_underway = True
                                async_start(self.ctx.trade_send(units_to_send))
                        elif button == TRADE_RECEIVE_1_BUTTON:
                            self.ctx.trade_underway = True
                            if self.supply_used != self.last_supply_used:
                                self.ctx.trade_response = None
                                async_start(self.ctx.trade_receive(1))
                            else:
                                self.ctx.trade_response = "?TradeFail Void Trade rejected: Not enough supply."
                        elif button == TRADE_RECEIVE_5_BUTTON:
                            self.ctx.trade_underway = True
                            if self.supply_used != self.last_supply_used:
                                self.ctx.trade_response = None
                                async_start(self.ctx.trade_receive(5))
                            else:
                                self.ctx.trade_response = "?TradeFail Void Trade rejected: Not enough supply."
                    else:
                        # The API returns no passengers for researching/training buildings,
                        # so we need to buffer the passengers each frame
                        self.last_trade_cargo = unit.passengers
                        # SC2 has no good means of detecting when a unit is queued while supply capped,
                        # so a supply buffer here is the best we can do
                        self.last_supply_used = self.supply_used
            game_state = controller1_state + (controller2_state << 15)

            if iteration == 160 and not game_state & 1:
                await self.chat_send("?SendMessage Warning: Archipelago unable to connect or has lost connection to " +
                                     "Starcraft 2 (This is likely a map issue)")

            if self.last_received_update < len(self.ctx.items_received):
                current_items = calculate_items(self.ctx)
                missions_beaten = self.missions_beaten_count()
                kerrigan_level = get_kerrigan_level(self.ctx, current_items, missions_beaten)
                await self.updateTerranTech(current_items)
                await self.updateZergTech(current_items, kerrigan_level)
                await self.updateProtossTech(current_items)
                self.last_received_update = len(self.ctx.items_received)

            if game_state & 1:
                if not self.game_running:
                    print("Archipelago Connected")
                    self.game_running = True

                if self.can_read_game:
                    if game_state & (1 << 1) and not self.mission_completed:
                        victory_location = get_location_id(self.mission_id, 0)
                        send_victory = (
                            self.mission_id in self.ctx.final_mission_ids and
                            len(self.ctx.final_locations) == len(self.ctx.checked_locations.union([victory_location]).intersection(self.ctx.final_locations))
                        )
                        
                        # Old slots don't have a location on victory
                        if not send_victory or self.ctx.slot_data_version >= 4:
                            print("Mission Completed")
                            await self.ctx.send_msgs(
                                [{"cmd": 'LocationChecks',
                                    "locations": [victory_location]}])
                            self.mission_completed = True

                        if send_victory:
                            print("Game Complete")
                            await self.ctx.send_msgs([{"cmd": 'StatusUpdate', "status": ClientStatus.CLIENT_GOAL}])
                            self.mission_completed = True
                            self.ctx.finished_game = True

                    for x, completed in enumerate(self.boni):
                        if not completed and game_state & (1 << (x + 2)):
                            await self.ctx.send_msgs(
                                [{"cmd": 'LocationChecks',
                                  "locations": [get_location_id(self.mission_id, x + 1)]}])
                            self.boni[x] = True
                    
                    # Send Void Trade results
                    if self.ctx.trade_response is not None:
                        await self.chat_send(self.ctx.trade_response)
                        self.ctx.trade_response = None
                        self.ctx.trade_underway = False
                else:
                    await self.chat_send("?SendMessage LostConnection - Lost connection to game.")

    def get_uncollected_objectives(self) -> typing.List[int]:
        result = [
            location % VICTORY_MODULO for location in
            self.ctx.uncollected_locations_in_mission(lookup_id_to_mission[self.mission_id])
        ]
        if self.mission_id in self.ctx.final_mission_ids and not self.ctx.finished_game and 0 not in result:
            # Goal isn't a regular location or the goal location has been collected and the game haven't been goaled yet.
            # Mark victory as uncollected so the goal can be sent back by the game
            result.append(0)
        return result

    def missions_beaten_count(self):
        return len([location for location in self.ctx.checked_locations if location % VICTORY_MODULO == 0])

    async def updateColors(self):
        await self.chat_send("?SetColor rr " + str(self.ctx.player_color_raynor))
        await self.chat_send("?SetColor ks " + str(self.ctx.player_color_zerg))
        await self.chat_send("?SetColor pz " + str(self.ctx.player_color_zerg_primal))
        await self.chat_send("?SetColor da " + str(self.ctx.player_color_protoss))
        await self.chat_send("?SetColor nova " + str(self.ctx.player_color_nova))
        self.ctx.pending_color_update = False

    async def updateTerranTech(self, current_items):
        terran_items = current_items[SC2Race.TERRAN]
        await self.chat_send("?GiveTerranTech " + " ".join(map(str, terran_items)))

    async def updateZergTech(self, current_items, kerrigan_level):
        zerg_items = current_items[SC2Race.ZERG]
        zerg_items = [value for index, value in enumerate(zerg_items) if index not in [ZergItemType.Level.flag_word, ZergItemType.Primal_Form.flag_word]]
        kerrigan_primal_by_items = kerrigan_primal(self.ctx, kerrigan_level)
        kerrigan_primal_bot_value = 1 if kerrigan_primal_by_items else 0
        await self.chat_send(f"?GiveZergTech {kerrigan_level} {kerrigan_primal_bot_value} " + ' '.join(map(str, zerg_items)))

    async def updateProtossTech(self, current_items):
        protoss_items = current_items[SC2Race.PROTOSS]
        await self.chat_send("?GiveProtossTech " + " ".join(map(str, protoss_items)))

def calc_unfinished_nodes(
        ctx: SC2Context
) -> typing.Tuple[typing.List[int], typing.Dict[int, typing.List[int]], typing.List[int], typing.Set[int]]:
    unfinished_missions: typing.Set[int] = set()

    available_missions, available_layouts, available_campaigns = calc_available_nodes(ctx)

    for mission_id in available_missions:
        objectives = set(ctx.locations_for_mission_id(mission_id))
        if objectives:
            objectives_completed = ctx.checked_locations & objectives
            if len(objectives_completed) < len(objectives):
                unfinished_missions.add(mission_id)
    
    return available_missions, available_layouts, available_campaigns, unfinished_missions

def is_mission_available(ctx: SC2Context, mission_id_to_check: int) -> bool:
    available_missions, _, _ = calc_available_nodes(ctx)

    return mission_id_to_check in available_missions

def calc_available_nodes(ctx: SC2Context) -> typing.Tuple[typing.List[int], typing.Dict[int, typing.List[int]], typing.List[int]]:
    available_missions: typing.List[int] = []
    available_layouts: typing.Dict[int, typing.List[int]] = {}
    available_campaigns: typing.List[int] = []

    beaten_missions = {mission_id for mission_id in ctx.mission_id_to_entry_rules if ctx.is_mission_completed(mission_id)}
    received_items: typing.Dict[int, int] = {}
    for network_item in ctx.items_received:
        received_items.setdefault(network_item.item, 0)
        received_items[network_item.item] += 1

    accessible_rules: typing.Set[int] = set()
    for campaign_idx, campaign in enumerate(ctx.custom_mission_order):
        available_layouts[campaign_idx] = []
        if campaign.entry_rule.is_accessible(beaten_missions, received_items, ctx.mission_id_to_entry_rules, accessible_rules, []):
            available_campaigns.append(campaign_idx)
            for layout_idx, layout in enumerate(campaign.layouts):
                if layout.entry_rule.is_accessible(beaten_missions, received_items, ctx.mission_id_to_entry_rules, accessible_rules, []):
                    available_layouts[campaign_idx].append(layout_idx)
                    for column in layout.missions:
                        for mission in column:
                            if mission.mission_id == -1:
                                continue
                            if mission.entry_rule.is_accessible(beaten_missions, received_items, ctx.mission_id_to_entry_rules, accessible_rules, []):
                                available_missions.append(mission.mission_id)

    return available_missions, available_layouts, available_campaigns

def check_game_install_path() -> bool:
    # First thing: go to the default location for ExecuteInfo.
    # An exception for Windows is included because it's very difficult to find ~\Documents if the user moved it.
    if is_windows:
        # The next five lines of utterly inscrutable code are brought to you by copy-paste from Stack Overflow.
        # https://stackoverflow.com/questions/6227590/finding-the-users-my-documents-path/30924555#
        import ctypes.wintypes
        CSIDL_PERSONAL = 5  # My Documents
        SHGFP_TYPE_CURRENT = 0  # Get current, not default value

        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, buf)
        documentspath: str = buf.value
        einfo = str(documentspath / Path("StarCraft II\\ExecuteInfo.txt"))
    else:
        einfo = str(bot.paths.get_home() / Path(bot.paths.USERPATH[bot.paths.PF]))

    # Check if the file exists.
    if os.path.isfile(einfo):

        # Open the file and read it, picking out the latest executable's path.
        with open(einfo) as f:
            content = f.read()
        if content:
            search_result = re.search(r" = (.*)Versions", content)
            if not search_result:
                sc2_logger.warning(f"Found {einfo}, but it was empty. Run SC2 through the Blizzard launcher, "
                                    "then try again.")
                return False
            base = search_result.group(1)

            if os.path.exists(base):
                executable = bot.paths.latest_executeble(Path(base).expanduser() / "Versions")

                # Finally, check the path for an actual executable.
                # If we find one, great. Set up the SC2PATH.
                if os.path.isfile(executable):
                    sc2_logger.info(f"Found an SC2 install at {base}!")
                    sc2_logger.debug(f"Latest executable at {executable}.")
                    os.environ["SC2PATH"] = base
                    sc2_logger.debug(f"SC2PATH set to {base}.")
                    return True
                else:
                    sc2_logger.warning(f"We may have found an SC2 install at {base}, but couldn't find {executable}.")
            else:
                sc2_logger.warning(f"{einfo} pointed to {base}, but we could not find an SC2 install there.")
    else:
        sc2_logger.warning(f"Couldn't find {einfo}. Run SC2 through the Blizzard launcher, then try again. "
                           f"If that fails, please run /set_path with your SC2 install directory.")
    return False


def is_mod_installed_correctly() -> bool:
    """Searches for all required files."""
    if "SC2PATH" not in os.environ:
        check_game_install_path()
    sc2_path: str = os.environ["SC2PATH"]
    mapdir = sc2_path / Path('Maps/ArchipelagoCampaign')
    mods = ["ArchipelagoCore", "ArchipelagoPlayer", "ArchipelagoPlayerSuper", "ArchipelagoPatches",
            "ArchipelagoTriggers", "ArchipelagoPlayerWoL", "ArchipelagoPlayerHotS",
            "ArchipelagoPlayerLotV", "ArchipelagoPlayerLotVPrologue", "ArchipelagoPlayerNCO"]
    modfiles = [sc2_path / Path("Mods/" + mod + ".SC2Mod") for mod in mods]
    wol_required_maps: typing.List[str] = ["WoL" + os.sep + mission.map_file + ".SC2Map" for mission in SC2Mission
                         if mission.campaign in (SC2Campaign.WOL, SC2Campaign.PROPHECY)]
    hots_required_maps: typing.List[str] = ["HotS" + os.sep + mission.map_file + ".SC2Map" for mission in campaign_mission_table[SC2Campaign.HOTS]]
    lotv_required_maps: typing.List[str] = ["LotV" + os.sep + mission.map_file + ".SC2Map" for mission in SC2Mission
                                            if mission.campaign in (SC2Campaign.LOTV, SC2Campaign.PROLOGUE, SC2Campaign.EPILOGUE)]
    nco_required_maps: typing.List[str] = ["NCO" + os.sep + mission.map_file + ".SC2Map" for mission in campaign_mission_table[SC2Campaign.NCO]]
    required_maps = wol_required_maps + hots_required_maps + lotv_required_maps + nco_required_maps
    needs_files = False

    # Check for maps.
    missing_maps: typing.List[str] = []
    for mapfile in required_maps:
        if not os.path.isfile(mapdir / mapfile):
            missing_maps.append(mapfile)
    if len(missing_maps) >= 19:
        sc2_logger.warning(f"All map files missing from {mapdir}.")
        needs_files = True
    elif len(missing_maps) > 0:
        for map in missing_maps:
            sc2_logger.debug(f"Missing {map} from {mapdir}.")
        sc2_logger.warning(f"Missing {len(missing_maps)} map files.")
        needs_files = True
    else:  # Must be no maps missing
        sc2_logger.debug(f"All maps found in {mapdir}.")

    # Check for mods.
    for modfile in modfiles:
        if os.path.isfile(modfile) or os.path.isdir(modfile):
            sc2_logger.debug(f"Archipelago mod found at {modfile}.")
        else:
            sc2_logger.warning(f"Archipelago mod could not be found at {modfile}.")
            needs_files = True

    # Final verdict.
    if needs_files:
        sc2_logger.warning(f"Required files are missing. Run /download_data to acquire them.")
        return False
    else:
        sc2_logger.debug(f"All map/mod files are properly installed.")
        return True


class DllDirectory:
    # Credit to Black Sliver for this code.
    # More info: https://docs.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setdlldirectoryw
    _old: typing.Optional[str] = None
    _new: typing.Optional[str] = None

    def __init__(self, new: typing.Optional[str]):
        self._new = new

    def __enter__(self):
        old = self.get()
        if self.set(self._new):
            self._old = old

    def __exit__(self, *args):
        if self._old is not None:
            self.set(self._old)

    @staticmethod
    def get() -> typing.Optional[str]:
        if sys.platform == "win32":
            n = ctypes.windll.kernel32.GetDllDirectoryW(0, None)
            buf = ctypes.create_unicode_buffer(n)
            ctypes.windll.kernel32.GetDllDirectoryW(n, buf)
            return buf.value
        # NOTE: other OS may support os.environ["LD_LIBRARY_PATH"], but this fix is windows-specific
        return None

    @staticmethod
    def set(s: typing.Optional[str]) -> bool:
        if sys.platform == "win32":
            return ctypes.windll.kernel32.SetDllDirectoryW(s) != 0
        # NOTE: other OS may support os.environ["LD_LIBRARY_PATH"], but this fix is windows-specific
        return False


def download_latest_release_zip(
    owner: str,
    repo: str,
    api_version: str,
    metadata: typing.Optional[str] = None,
    force_download=False
) -> typing.Tuple[str, typing.Optional[str]]:
    """Downloads the latest release of a GitHub repo to the current directory as a .zip file."""
    import requests

    headers = {"Accept": 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{api_version}"

    try:
        r1 = requests.get(url, headers=headers)
        if r1.status_code == 200:
            latest_metadata = r1.json()
            cleanup_downloaded_metadata(latest_metadata)
            latest_metadata = str(latest_metadata)
            # sc2_logger.info(f"Latest version: {latest_metadata}.")
        else:
            sc2_logger.warning(f"Status code: {r1.status_code}")
            sc2_logger.warning(f"Failed to reach GitHub. Could not find download link.")
            sc2_logger.warning(f"text: {r1.text}")
            return "", metadata

        if (force_download is False) and (metadata == latest_metadata):
            sc2_logger.info("Latest version already installed.")
            return "", metadata

        sc2_logger.info(f"Attempting to download latest version of API version {api_version} of {repo}.")
        download_url = r1.json()["assets"][0]["browser_download_url"]

        r2 = requests.get(download_url, headers=headers)
        if r2.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r2.content)):
            tempdir = tempfile.gettempdir()
            file = tempdir + os.sep + f"{repo}.zip"
            with open(file, "wb") as fh:
                fh.write(r2.content)
            sc2_logger.info(f"Successfully downloaded {repo}.zip. Installing...")
            return file, latest_metadata
        else:
            sc2_logger.warning(f"Status code: {r2.status_code}")
            sc2_logger.warning("Download failed.")
            sc2_logger.warning(f"text: {r2.text}")
            return "", metadata
    except requests.ConnectionError:
        sc2_logger.warning(f"Failed to reach GitHub. Could not find download link.")
        return "", metadata


def cleanup_downloaded_metadata(medatada_json: dict) -> None:
    for asset in medatada_json['assets']:
        del asset['download_count']


def is_mod_update_available(owner: str, repo: str, api_version: str, metadata: str) -> bool:
    import requests

    headers = {"Accept": 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{api_version}"

    try:
        r1 = requests.get(url, headers=headers)
        if r1.status_code == 200:
            latest_metadata = r1.json()
            cleanup_downloaded_metadata(latest_metadata)
            latest_metadata = str(latest_metadata)
            if metadata != latest_metadata:
                return True
            else:
                return False

        else:
            sc2_logger.warning(f"Failed to reach GitHub while checking for updates.")
            sc2_logger.warning(f"Status code: {r1.status_code}")
            sc2_logger.warning(f"text: {r1.text}")
            return False
    except requests.ConnectionError:
        sc2_logger.warning(f"Failed to reach GitHub while checking for updates.")
        return False


def get_location_offset(mission_id: int) -> int:
    return SC2WOL_LOC_ID_OFFSET if mission_id <= SC2Mission.ALL_IN.id \
        else (SC2HOTS_LOC_ID_OFFSET - SC2Mission.ALL_IN.id * VICTORY_MODULO)

def get_location_id(mission_id: int, objective_id: int) -> int:
    return get_location_offset(mission_id) + mission_id * VICTORY_MODULO + objective_id


_has_forced_save = False
def force_settings_save_on_close() -> None:
    """
    Settings has an existing auto-save feature, but it only triggers if a new key was introduced.
    Force it to mark things as changed by introducing a new key and then cleaning up.
    """
    global _has_forced_save
    if _has_forced_save:
        return
    SC2World.settings.update({'invalid_attribute': True})
    del SC2World.settings.invalid_attribute
    _has_forced_save = True


def launch():
    colorama.init()
    asyncio.run(main())
    colorama.deinit()
