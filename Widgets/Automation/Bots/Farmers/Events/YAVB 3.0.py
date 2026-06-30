"""
Yet Another Vaettir Bot (Y.A.V.B) 3.0 - BottingTree edition

Port of the legacy "YAVB 2.0.py" (classic Botting/FSM stack) onto the new
BottingTree behavior-tree runtime.

Design decisions (agreed with the user):
- Single account (no multibox).
- Combat is NOT delegated to headless HeroAI. Instead the original custom
  Shadow Form builds (SF_Ass_vaettir / SF_Mes_vaettir) are kept and driven by a
  dedicated "SFBuildCaster" service tree. This preserves the precise Shadow
  Form / Heart of Shadow timing and the bot<->build signalling
  (SetKillingRoutine / SetStuckSignal / SetRoutineFinished / CastHeartOfShadow)
  that the legacy bot relied on.

The SFBuildCaster drives ONE persistent ProcessSkillCasting generator across
ticks (recreated only when it is exhausted or the build changes), mirroring the
long-lived `yield from build.ProcessSkillCasting()` loop used by the stock
Upkeepers. This honours the build's own aftercast/wait throttles - driving a
fresh generator every tick (advancing only to the first yield) discards those
throttles and makes the build re-issue upkeep casts every frame, which roots the
player (movement pauses unconditionally while casting) and stalls movement.

Loop control flow (faithful to the legacy bot):
- repeat=False (no built-in full-list repeater). The Reset Farm Loop step hops
  Bjora -> Jaga and restarts the planner from STEP_FARM, so the normal cycle
  re-runs ONLY the farm leg (legacy ResetFarmLoop + JumpToStepName).
- Death routing: the custom DeathRecovery service (replacing BottingTree's
  built-in PartyWipeRecoveryService) returns to the outpost on a wipe and
  restarts from STEP_TOWN for a full reset (legacy on_death -> Town Routines).
  The built-in recovery would instead resume STEP_FARM in the wrong map.

Module layout (the restructure designed for future extension):
  - Config            : all tunable constants in one place.
  - State             : every cross-tick / cross-node global on ONE `STATE`
                        object (no scattered `global` statements).
  - Builds            : a BUILD_REGISTRY (profession -> build factory) plus the
                        CombatSignals adapter. Adding a build = one registry
                        entry; SF-specific signalling is reached through the
                        adapter so non-SF builds simply no-op (no isinstance
                        checks anywhere else).
  - InventoryManager  : a single class owning the whole inventory concern
                        (settings model, persistence, UI tab, handler mapping,
                        town / post-kill / low-inventory planner nodes). Swap or
                        rework inventory = replace this one class; the planner
                        only touches its small node-builder interface.
  - Loot / Combat / Stuck / Death : the service + combat nodes.
  - Planner steps + wiring + main.
"""

from __future__ import annotations

import json
import os
from typing import Callable, List, Tuple

import Py4GW
import PyImGui

from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.IniManager import IniManager
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree

from Py4GWCoreLib import Routines, GLOBAL_CACHE, Agent, Item, Utils, Map, Player, ThrottledTimer, LootConfig, ActionQueueManager
from Py4GWCoreLib.py4gwcorelib_src.Console import ConsoleLog
from Py4GWCoreLib.py4gwcorelib_src.AutoInventoryHandler import AutoInventoryHandler
from Py4GWCoreLib.enums import ModelID, Range, TitleID

from Py4GWCoreLib.BuildMgr import BuildMgr
from Py4GWCoreLib.Builds.Mesmer.Me_A.SF_Mes_vaettir import SF_Mes_vaettir
from Py4GWCoreLib.Builds.Assassin.A_Me.SF_Ass_vaettir import SF_Ass_vaettir

from Py4GWCoreLib.routines_src.BehaviourTrees import BT as RoutinesBT
from Sources.ApoSource.ApoBottingLib import wrappers as BT


# region config (all tunable constants)
MODULE_NAME = "YAVB 3.0"
INI_PATH = "Widgets/Automation/Bots/Farmers/Events"
INI_FILENAME = "YAVB3.ini"

# Inventory settings persist to a self-contained JSON file (see InventoryManager).
# We deliberately do NOT use Database.Settings here: its global cache is filled
# from disk by a flush callback on a different thread (PyCallback.Phase.Data), so
# the FIRST write of a brand-new key from the widget/draw thread hits SQLite
# directly on the wrong thread ("SQLite objects created in a thread can only be
# used in that same thread"). Plain file I/O has no such constraint and needs no
# account email.

# Deposit only Glacial Stones (the AutoInventoryHandler deposits by category, so
# this is done per-model). Gold withdrawn from storage to buy kits (we otherwise
# deposit all gold).
GLACIAL_STONE_MODEL = ModelID.Glacial_Stone.value
KIT_WITHDRAW_GOLD = 5000

# Map ids (from legacy bot)
LONGEYES_LEDGE = 650
BJORA_MARCHES = 482
JAGA_MORAINE = 546

# Planner step names (the tuple keys returned by get_execution_steps). Defined
# once because they double as restart targets for the loop control flow:
#   - the farm-only loop restarts from STEP_FARM (Reset Farm Loop step), and
#   - the death recovery restarts from STEP_TOWN (full reset after a wipe).
STEP_INITIALIZE = "Initialize Bot"
STEP_TOWN = "Town Routines"
STEP_TRAVERSE = "Traverse Bjora Marches"
STEP_FARM = "Jaga Moraine Farm"
STEP_RESET_LOOP = "Reset Farm Loop"

# Merchant location in Longeyes Ledge
MERCHANT_XY: Tuple[float, float] = (-23110, 14942)

# Items kept (never sold) during the merchant pass: ID/Salvage kits + cupcakes.
_SELL_EXCLUDE = [
    ModelID.Superior_Identification_Kit.value,
    ModelID.Salvage_Kit.value,
    ModelID.Birthday_Cupcake.value,
]

# Looting (loop-until-clear; honors the Loot Manager via the LootConfig singleton)
LOOT_RANGE = Range.Spellcast.value          # filter radius around the player each poll
LOOT_CLEAR_POLLS_REQUIRED = 3               # consecutive empty polls => pile considered cleared
LOOT_COMPLETION_TIMEOUT_MS = 40000          # overall safety cap for one post-kill loot pass
LOOT_FIRST_DROP_GRACE_MS = 4000             # if no wanted drop appears within this, end a dry kill
# endregion


# region state (every cross-tick / cross-node global on one object)
class _RoutineFlags:
    """Coordination flags shared between the planner combat nodes and the stuck
    handler service (the legacy module-level booleans, grouped). `reset()` is the
    single place the farm leg re-arms them."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.in_waiting = False
        self.in_killing = False
        self.finished = False
        self.in_looting = False


class _BotState:
    """Single owner of all mutable bot state. Replaces a dozen module globals so
    nodes/services mutate attributes (no `global` statements) and there is one
    obvious place to inspect/reset runtime state."""

    def __init__(self) -> None:
        self.tree: BottingTree | None = None
        self.initialized = False
        self.inv_loaded = False
        self.ini_key = ""
        self.flags = _RoutineFlags()
        # The active custom build instance (assigned by AssignBuild, ticked by
        # the SFBuildCaster service) and its persistent ProcessSkillCasting
        # generator state (recreated only on exhaustion / build change).
        self.build: BuildMgr | None = None
        self.caster_gen = None
        self.caster_build: BuildMgr | None = None


STATE = _BotState()
# endregion


# region debug log helper
def _log(reason: str, extra: str = "", message_type=Py4GW.Console.MessageType.Info) -> None:
    map_id = Map.GetMapID()
    map_name = Map.GetMapName(map_id) if map_id else "Unknown"
    msg = f"{reason} | map_id={map_id} map='{map_name}'"
    if extra:
        msg = f"{msg} | {extra}"
    ConsoleLog("YAVB3", msg, message_type, True)
# endregion


# region behavior-tree primitives
def _CoroutineNode(name: str, make_gen: Callable[[], object]) -> BehaviorTree:
    """
    Drive a Py4GW coroutine/generator across ticks until it is exhausted.

    A fresh generator is built per planner execution (via SubtreeNode), stepped
    once per tick, and the node returns SUCCESS on StopIteration. Use this for
    one-shot generator actions such as LoadSkillBar or CastHeartOfShadow.
    """

    def _build(_node: BehaviorTree.Node) -> BehaviorTree:
        state: dict[str, object] = {"gen": None}

        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            if state["gen"] is None:
                state["gen"] = make_gen()
            try:
                next(state["gen"])  # type: ignore[arg-type]
                return BehaviorTree.NodeState.RUNNING
            except StopIteration:
                return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(name=f"{name}Action", action_fn=_fn, aftercast_ms=0)
        )

    return BehaviorTree(BehaviorTree.SubtreeNode(name=name, subtree_fn=_build))


def Optional(node, name: str = "Optional") -> BehaviorTree:
    """Wrap a node so FAILURE becomes SUCCESS (RUNNING preserved).

    Used for non-essential steps (merchant/restock) so a single failing node
    can never bring down the whole planner sequence.
    """
    return BehaviorTree(
        BehaviorTree.SelectorNode(
            name=name,
            children=[node, BehaviorTree.SucceederNode(name=f"{name}Ok")],
        )
    )
# endregion


# region builds: registry + combat-signal protocol
# Profession -> custom build factory. This is the ONE place to add a build: drop
# a class here and (optionally) implement the CombatSignals protocol methods so
# the planner can coordinate with it. Everything else dispatches through this.
BUILD_REGISTRY: dict[str, Callable[..., BuildMgr]] = {
    "Assassin": SF_Ass_vaettir,
    "Mesmer": SF_Mes_vaettir,
}


def _build_factory_for_player() -> Tuple[Callable[..., BuildMgr] | None, str]:
    """Resolve the registered build factory for the local character's primary
    profession (and the profession name for logging). The factory is None if the
    profession is unsupported."""
    profession, _ = Agent.GetProfessionNames(Player.GetAgentID())
    return BUILD_REGISTRY.get(profession), profession


class CombatSignals:
    """Optional combat-coordination hooks the planner/services send to the active
    build (STATE.build). A build participates by implementing the matching method
    (the SF builds do); builds that don't simply no-op. This is the seam that
    lets new builds be added without touching the combat nodes - they used to do
    `isinstance(build, SF_...)` checks everywhere, which is now centralised here.

      set_killing(active)  -> SetKillingRoutine    (in/out of the kill routine)
      set_finished(done)   -> SetRoutineFinished   (kill routine cleared)
      set_stuck(count)     -> SetStuckSignal       (escalate the unstuck escape)
      supports_escape()    -> has CastHeartOfShadow (an active unstuck/aggro push)
      cast_escape()        -> CastHeartOfShadow()   (the escape generator)
    """

    @staticmethod
    def set_killing(active: bool) -> None:
        build = STATE.build
        if hasattr(build, "SetKillingRoutine"):
            build.SetKillingRoutine(active)  # type: ignore[union-attr]

    @staticmethod
    def set_finished(done: bool) -> None:
        build = STATE.build
        if hasattr(build, "SetRoutineFinished"):
            build.SetRoutineFinished(done)  # type: ignore[union-attr]

    @staticmethod
    def set_stuck(count: int) -> None:
        build = STATE.build
        if hasattr(build, "SetStuckSignal"):
            build.SetStuckSignal(count)  # type: ignore[union-attr]

    @staticmethod
    def supports_escape() -> bool:
        return hasattr(STATE.build, "CastHeartOfShadow")

    @staticmethod
    def cast_escape():
        return STATE.build.CastHeartOfShadow()  # type: ignore[union-attr]


def _assign_build_fn(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
    factory, profession = _build_factory_for_player()
    if factory is None:
        ConsoleLog(
            MODULE_NAME,
            f"Unsupported profession '{profession}'. Stopping.",
            Py4GW.Console.MessageType.Error,
            True,
        )
        return BehaviorTree.NodeState.FAILURE
    STATE.build = factory()
    _log("AssignBuild", f"profession={profession} build={STATE.build.build_name}")
    return BehaviorTree.NodeState.SUCCESS


def AssignBuild() -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.ActionNode(name="AssignBuild", action_fn=_assign_build_fn, aftercast_ms=0)
    )


def EquipSkillBar() -> BehaviorTree:
    # Load the skillbar FIRST (resolving the template by profession via a cheap
    # match_only build), THEN construct the real build in AssignBuild so its
    # __init__ slot caching (wastrels_demise_slot / arcane_echo_slot via
    # GLOBAL_CACHE.SkillBar.GetSlotBySkillID) reads a loaded bar.
    def _load(_node: BehaviorTree.Node) -> BehaviorTree:
        factory, _ = _build_factory_for_player()
        if factory is None:
            return BT.Failer(name="EquipSkillBarUnsupported")
        template = factory(match_only=True).template_code
        return BT.LoadSkillbar(template)

    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Equip Skill Bar",
            children=[
                BehaviorTree.SubtreeNode(name="LoadSkillbarByProfession", subtree_fn=_load),
                AssignBuild(),
            ],
        )
    )
# endregion


# region SF build service tree (replaces legacy build_ticker)
def _sf_build_caster_fn(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
    # Drive ONE persistent ProcessSkillCasting generator across ticks (recreated
    # only on exhaustion / map-invalid / build change). This mirrors the stock
    # `yield from build.ProcessSkillCasting()` loop and honours the build's own
    # aftercast/wait throttles, so the player only roots when a buff actually
    # needs (re)casting instead of being held by a per-tick cast spam.
    build = STATE.build
    # Only cast in explorable maps (Bjora traverse needs SF upkeep, Jaga is the
    # farm). Never cast in outposts - no skills needed there.
    if (build is None
            or not Routines.Checks.Map.MapValid()
            or not Routines.Checks.Map.IsExplorable()):
        STATE.caster_gen = None
        STATE.caster_build = None
        return BehaviorTree.NodeState.RUNNING
    # While looting, stop casting SF upkeep: continuous casts root the player
    # (movement.py pauses movement unconditionally while IsCasting) and fight the
    # loot walk, so we reach fewer drops. Legacy did the same (Disable
    # build_ticker before LootItems). Enemies are dead post-kill, so the buff is
    # not needed here. Keep the generator alive so it resumes after looting.
    if STATE.flags.in_looting:
        return BehaviorTree.NodeState.RUNNING
    if STATE.caster_gen is None or STATE.caster_build is not build:
        STATE.caster_gen = build.ProcessSkillCasting()
        STATE.caster_build = build
    try:
        next(STATE.caster_gen)
    except StopIteration:
        STATE.caster_gen = None
    return BehaviorTree.NodeState.RUNNING


def SFBuildCaster() -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.ActionNode(
            name="SFBuildCaster",
            action_fn=_sf_build_caster_fn,
            aftercast_ms=0,
        )
    )
# endregion


# region inventory manager (the whole inventory concern behind one swappable class)
class InventoryManager:
    """Owns everything inventory: the per-rarity disposition model, its UI tab,
    persistence (a self-contained JSON settings file), the AutoInventoryHandler
    mapping, and the planner nodes that run in town / post-kill.

    The planner only ever calls the node-builder methods
    (configure_node / town_node / post_kill_node / needs_management_node) and the
    main loop calls load / apply_to_handler / draw_tab. To rework or replace the
    inventory system, swap THIS class for one exposing the same small interface -
    no planner step needs to change.

    Per-rarity disposition: each item category does exactly ONE of
    Salvage / Deposit / Sell (mutually exclusive), chosen via radio buttons. The
    UI is a thin layer over the underlying AutoInventoryHandler bool flags:
        Salvage -> salvage_<x>=True,  deposit_<x>=False
        Deposit -> salvage_<x>=False, deposit_<x>=True
        Sell    -> both False (left in the bags and sold with the rest)
    Row = (label, salvage_attr | None, deposit_attr | None, options). A None attr
    means that action is not offered for the category. `deposit_glacial_stones`
    is a YAVB3-custom flag handled by _deposit_glacial_gen (the handler deposits
    by category, not per-model), not a 1:1 AutoInventoryHandler attribute.
    """

    SALVAGE, DEPOSIT, SELL = "Salvage", "Deposit", "Sell"
    # Radio buttons share an int selection per row; map actions <-> ints.
    ACTION_INT = {SALVAGE: 0, DEPOSIT: 1, SELL: 2}
    INT_ACTION = {0: SALVAGE, 1: DEPOSIT, 2: SELL}

    ROWS = [
        ("Whites",         "salvage_whites",         None,                     [SALVAGE, SELL]),
        ("Blues",          "salvage_blues",          "deposit_blues",          [SALVAGE, DEPOSIT, SELL]),
        ("Purples",        "salvage_purples",        "deposit_purples",        [SALVAGE, DEPOSIT, SELL]),
        ("Golds",          "salvage_golds",          "deposit_golds",          [SALVAGE, DEPOSIT, SELL]),
        ("Glacial Stones", "salvage_glacial_stones", "deposit_glacial_stones", [SALVAGE, DEPOSIT, SELL]),
        ("Materials",      None,                     "deposit_materials",      [DEPOSIT, SELL]),
        ("Event Items",    None,                     "deposit_event_items",    [DEPOSIT]),
    ]

    # Default disposition per flag. Mirrors the legacy hardcoded behaviour so
    # nothing changes until a radio is toggled.
    DEFAULTS = {
        "salvage_whites": True,
        "salvage_blues": True,
        "salvage_purples": True,
        "salvage_golds": False,
        "salvage_glacial_stones": False,
        "deposit_blues": False,
        "deposit_purples": False,
        "deposit_golds": False,
        "deposit_glacial_stones": True,
        "deposit_materials": True,
        "deposit_event_items": True,
    }

    TABLE_FLAGS = (
        PyImGui.TableFlags.Borders | PyImGui.TableFlags.RowBg | PyImGui.TableFlags.SizingStretchSame
    )

    # Integer settings: (attr, label, default, min, max). Editable in the
    # Inventory tab, persisted to the JSON settings file alongside the rarity
    # dispositions. The two kit counts are "keep stocked to this count in town":
    # whenever a kit type is below its count, the bot tops it up at the merchant.
    # free_inventory_slots is the resign threshold: the bot gives up the run and
    # routes to town to sell/deposit when free slots drop below it.
    INT_SETTINGS = [
        ("restock_id_kits",      "Identify Kits",                 2, 1, 50),
        ("restock_salvage_kits", "Salvage Kits",                  3, 1, 50),
        ("free_inventory_slots", "Free Slots (resign below)",     6, 1, 60),
        ("salvage_throttle_ms",  "Salvage delay ms (anti-disc.)", 750, 0, 3000),
    ]

    # Mods to KEEP: items carrying any selected mod are never salvaged (post-kill
    # OR in town) and never sold; instead they are deposited to Xunlai storage the
    # next time the character is in the outpost. The selection is a FREE list of
    # ItemUpgrade member names (chosen from ALL mods via the Mods-tab combobox), not
    # a fixed set. Detection is by the DECODED upgrade id (via ItemModifierParser),
    # so it is robust regardless of the raw modifier encoding.
    #
    # Legacy migration: the previous build stored five fixed bool keys; map them to
    # ItemUpgrade member names so an existing selection survives the move to a list.
    _LEGACY_KEEP_MAP = {
        "keep_aptitude_not_attitude":  "AptitudeNotAttitude",
        "keep_forget_me_not":          "ForgetMeNot",
        "keep_live_for_today":         "LiveForToday",
        "keep_like_a_rolling_stone":   "LikeARollingStone",
        "keep_shield_handle_devotion": "OfDevotion",
    }
    # When True, an item is only kept if its matching mod is at its MAXIMUM value
    # (Upgrade.is_maxed). Fixed-value inscriptions always report maxed=True, so this
    # only filters value-ranged mods (e.g. of Devotion's Health-while-enchanted).
    KEEP_ONLY_MAX_DEFAULT = False

    def __init__(self) -> None:
        self.settings: dict[str, bool] = {}
        self.int_settings: dict[str, int] = {}
        # ItemUpgrade member names the user chose to keep (free list, persisted).
        self.keep_mod_names: list[str] = []
        self.keep_only_max: bool = self.KEEP_ONLY_MAX_DEFAULT
        # Lazily-built, cached [(member_name, display_name)] for the combobox.
        self._mod_choices: list[Tuple[str, str]] | None = None
        # Transient Mods-tab UI state (not persisted).
        self._mod_filter: str = ""
        self._mod_combo_index: int = 0

    # --- settings model (persisted to one JSON file; self-contained file I/O,
    # so no DB cross-thread / account-email constraints) ----------------------
    def defaults(self) -> dict:
        return dict(self.DEFAULTS)

    def int_defaults(self) -> dict:
        return {attr: default for attr, _label, default, _mn, _mx in self.INT_SETTINGS}

    def _int_bounds(self, attr: str) -> Tuple[int, int]:
        for a, _label, _default, mn, mx in self.INT_SETTINGS:
            if a == attr:
                return mn, mx
        return 0, 1_000_000

    def _clamp(self, attr: str, value: int) -> int:
        mn, mx = self._int_bounds(attr)
        return max(mn, min(mx, int(value)))

    def _int(self, attr: str) -> int:
        """Configured value for an int setting (falls back to the default)."""
        return self.int_settings.get(attr, self.int_defaults()[attr])

    def _settings_path(self) -> str:
        base = Py4GW.Console.get_projects_path()
        return os.path.join(base, "Widgets", "Config", "yavb3_inventory.json")

    def load(self) -> bool:
        """Load all inventory settings from the JSON file. Missing file -> the
        defaults (already applied below). Returns False only on a corrupt /
        unreadable file (defaults are still applied, so the bot stays usable)."""
        self.settings = self.defaults()
        self.int_settings = self.int_defaults()
        self.keep_mod_names = []
        self.keep_only_max = self.KEEP_ONLY_MAX_DEFAULT
        try:
            path = self._settings_path()
            if not os.path.exists(path):
                return True
            with open(path, "r") as f:
                data = json.load(f)
            for attr in self.defaults():
                if attr in data:
                    self.settings[attr] = bool(data[attr])
            for attr in self.int_defaults():
                if attr in data:
                    self.int_settings[attr] = self._clamp(attr, int(data[attr]))
            raw_names = data.get("keep_mod_names")
            if isinstance(raw_names, list):
                self.keep_mod_names = [str(n) for n in raw_names]
            else:
                # Migrate the legacy fixed bool keys into the new free list.
                self.keep_mod_names = [member for key, member in self._LEGACY_KEEP_MAP.items()
                                       if data.get(key)]
            if "keep_only_max" in data:
                self.keep_only_max = bool(data["keep_only_max"])
            return True
        except Exception as exc:
            _log("InvLoad", f"failed to read settings file: {exc!r}", Py4GW.Console.MessageType.Error)
            return False

    def _save(self) -> None:
        data: dict = {k: bool(v) for k, v in self.settings.items()}
        data.update({k: int(v) for k, v in self.int_settings.items()})
        data["keep_mod_names"] = list(self.keep_mod_names)
        data["keep_only_max"] = bool(self.keep_only_max)
        try:
            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            _log("InvSet", f"failed to write settings file: {exc!r}", Py4GW.Console.MessageType.Error)

    def set(self, attr: str, value: bool) -> None:
        """Update one disposition flag and persist all settings to the JSON file."""
        self.settings[attr] = bool(value)
        self._save()

    def set_int(self, attr: str, value: int) -> None:
        """Update one int setting (clamped) and persist all settings."""
        self.int_settings[attr] = self._clamp(attr, value)
        self._save()

    def add_keep_mod(self, member_name: str) -> None:
        """Add an ItemUpgrade member name to the keep list (deduped) and persist."""
        if member_name and member_name not in self.keep_mod_names:
            self.keep_mod_names.append(member_name)
            self._save()

    def remove_keep_mod(self, member_name: str) -> None:
        """Remove an ItemUpgrade member name from the keep list and persist."""
        if member_name in self.keep_mod_names:
            self.keep_mod_names.remove(member_name)
            self._save()

    def set_keep_only_max(self, value: bool) -> None:
        """Toggle 'keep only max-value mods' and persist all settings."""
        self.keep_only_max = bool(value)
        self._save()

    def _row_action(self, salv_attr, dep_attr) -> str:
        """Current disposition of a row, derived from the stored flags (salvage wins)."""
        if salv_attr and self.settings.get(salv_attr):
            return self.SALVAGE
        if dep_attr and self.settings.get(dep_attr):
            return self.DEPOSIT
        return self.SELL

    def _set_action(self, salv_attr, dep_attr, action: str) -> None:
        """Apply a radio choice by writing the underlying flags (mutually excl.)."""
        if salv_attr:
            self.set(salv_attr, action == self.SALVAGE)
        if dep_attr:
            self.set(dep_attr, action == self.DEPOSIT)

    def apply_to_handler(self) -> None:
        """Push the current settings onto the AutoInventoryHandler singleton.
        `module_active` stays False: YAVB3 drives the handler manually from the
        planner (town / post-kill), so the InventoryPlus widget must not
        double-drive it."""
        h = AutoInventoryHandler()
        eff = self.settings or self.defaults()
        for attr, val in eff.items():
            if attr in ("deposit_glacial_stones", "salvage_glacial_stones"):
                continue  # custom YAVB3 keys, mapped explicitly below (not 1:1 handler attrs)
            setattr(h, attr, bool(val))
        # "Salvage" for Glacial Stones maps to the handler's rare-materials salvage.
        h.salvage_rare_materials = bool(eff.get("salvage_glacial_stones", False))
        # Identify is derived, not user-controlled: only ID the rarities we
        # actually salvage (a blue/purple/gold must be identified to salvage it
        # by rarity); never identify otherwise. Whites/greens are never identified.
        h.id_whites = False
        h.id_blues = bool(eff.get("salvage_blues", False))
        h.id_purples = bool(eff.get("salvage_purples", False))
        h.id_golds = bool(eff.get("salvage_golds", False))
        h.id_greens = False
        # Identifying is required to read a weapon's hidden mods. When any
        # keep-mod is enabled, force-ID the coloured rarities so kept mods are
        # detectable even when that rarity is set to Sell/Deposit (not Salvage).
        if self.keep_mod_names:
            h.id_blues = True
            h.id_purples = True
            h.id_golds = True
        # Trophies/dyes have no table row, so never deposit them via the handler
        # (Glacial Stones are deposited per-model by _deposit_glacial_gen). The
        # coloured-rarity deposits (blues/purples/golds) ARE table-controlled.
        h.deposit_trophies = False
        h.deposit_dyes = False
        # Always deposit all gold; we withdraw a fixed amount only when buying kits.
        h.keep_gold = 0
        h.module_active = False

    # --- UI ---------------------------------------------------------------
    @staticmethod
    def _input_int(label: str, current: int) -> int:
        """PyImGui.input_int may return an int, a (changed, value) tuple, or None
        depending on the binding; normalise to a plain int (mirror of GWUI Test)."""
        result = PyImGui.input_int(label, int(current))
        if isinstance(result, tuple):
            return int(result[1] if len(result) >= 2 else result[0])
        if result is None:
            return int(current)
        return int(result)

    def draw_tab(self) -> None:
        """Contents of the 'Inventory' tab inside the bot window. The BottingTree
        UI wraps this in begin_tab_item/end_tab_item (passed via
        draw_window(extra_tabs=...)), so this draws only the controls. Toggling a
        radio applies immediately to the AutoInventoryHandler and persists."""
        if PyImGui.begin_table("YAVB3InventoryTable", 4, self.TABLE_FLAGS):
            PyImGui.table_setup_column("Item")
            PyImGui.table_setup_column("Salvage")
            PyImGui.table_setup_column("Deposit")
            PyImGui.table_setup_column("Sell")
            PyImGui.table_headers_row()
            for label, salv_attr, dep_attr, options in self.ROWS:
                cur_int = self.ACTION_INT[self._row_action(salv_attr, dep_attr)]
                new_int = cur_int
                PyImGui.table_next_row()
                PyImGui.table_next_column()
                PyImGui.text(label)
                # One radio per action column; they share new_int so exactly one
                # is selected per row. Actions not valid for the category are
                # shown as greyed, non-selectable radios.
                for action in (self.SALVAGE, self.DEPOSIT, self.SELL):
                    PyImGui.table_next_column()
                    if action in options:
                        new_int = PyImGui.radio_button(f"##{action}_{label}", new_int, self.ACTION_INT[action])
                    else:
                        PyImGui.begin_disabled(True)
                        PyImGui.radio_button(f"##{action}_{label}", new_int, self.ACTION_INT[action])
                        PyImGui.end_disabled()
                if new_int != cur_int:
                    self._set_action(salv_attr, dep_attr, self.INT_ACTION[new_int])
                    self.apply_to_handler()
            PyImGui.end_table()

        # Integer settings: kit restock targets + the resign free-slot threshold.
        PyImGui.separator()
        PyImGui.text("Kit targets (restock in town when below) & resign threshold")
        for attr, label, _default, _mn, _mx in self.INT_SETTINGS:
            cur = self._int(attr)
            new = self._input_int(f"{label}##{attr}", cur)
            if new != cur:
                self.set_int(attr, new)

    def _mod_choice_cache(self) -> List[Tuple[str, str]]:
        """All selectable mods as (ItemUpgrade member name, display name), sorted by
        display name, built once and cached. Source is the library's full
        concrete-mod list (_UPGRADES); each class exposes its ItemUpgrade id and a
        static display name. Deduped by upgrade id. Returns [] if the library can't
        be read (the tab then just shows an empty combobox)."""
        if self._mod_choices is not None:
            return self._mod_choices
        choices: List[Tuple[str, str]] = []
        try:
            from Py4GWCoreLib.item_mods_src.upgrades import _UPGRADES
            from Py4GWCoreLib.item_mods_src.types import ItemUpgrade
            seen = set()
            for cls in _UPGRADES:
                upg_id = getattr(cls, "id", None)
                if not isinstance(upg_id, ItemUpgrade) or upg_id in seen or upg_id.name == "Unknown":
                    continue
                seen.add(upg_id)
                try:
                    display = cls.get_static_name() or upg_id.name
                except Exception:
                    display = upg_id.name
                choices.append((upg_id.name, str(display)))
            choices.sort(key=lambda c: c[1].lower())
        except Exception as exc:
            _log("KeepMods", f"could not build mod list: {exc!r}", Py4GW.Console.MessageType.Error)
        self._mod_choices = choices
        return choices

    def draw_mods_tab(self) -> None:
        """Contents of the 'Mods' tab: a FREE list of mods to KEEP. Pick any mod
        from the combobox (all mods, optionally filtered) and Add it; each kept mod
        has a Delete button. An item carrying a kept mod is never salvaged or sold
        and is deposited to Xunlai storage the next time in the outpost. The
        selection persists to the JSON settings file. Add/Delete also re-apply the
        handler flags (the coloured rarities get force-identified, required to read
        the mods)."""
        only_max = bool(self.keep_only_max)
        new_only_max = PyImGui.checkbox("Only keep MAX-value mods", only_max)
        if new_only_max != only_max:
            self.set_keep_only_max(new_only_max)
        PyImGui.text("(fixed inscriptions always count as max)")
        PyImGui.separator()

        choices = self._mod_choice_cache()
        display_by_name = dict(choices)

        # Add row: optional text filter + combobox over all mods + Add button.
        PyImGui.text("Add a mod to keep:")
        self._mod_filter = PyImGui.input_text("Filter##yavb3_modfilter", self._mod_filter, 64)
        flt = self._mod_filter.strip().lower()
        filtered = [c for c in choices if flt in c[1].lower()] if flt else choices
        if filtered:
            if self._mod_combo_index >= len(filtered):
                self._mod_combo_index = 0
            self._mod_combo_index = PyImGui.combo(
                "##yavb3_modcombo", self._mod_combo_index, [disp for _name, disp in filtered]
            )
            if PyImGui.button("Add##yavb3_modadd"):
                self.add_keep_mod(filtered[self._mod_combo_index][0])
                self.apply_to_handler()
        else:
            PyImGui.text("(no mod matches the filter)")

        PyImGui.separator()
        PyImGui.text("Kept mods:")
        if not self.keep_mod_names:
            PyImGui.text("  (none - nothing is kept)")
        elif PyImGui.begin_table("YAVB3KeepModList", 2, self.TABLE_FLAGS):
            PyImGui.table_setup_column("Mod")
            PyImGui.table_setup_column("")
            for member_name in list(self.keep_mod_names):
                PyImGui.table_next_row()
                PyImGui.table_next_column()
                PyImGui.text(display_by_name.get(member_name, member_name))
                PyImGui.table_next_column()
                if PyImGui.button(f"Delete##yavb3_del_{member_name}"):
                    self.remove_keep_mod(member_name)
                    self.apply_to_handler()
            PyImGui.end_table()

    # --- planner nodes ----------------------------------------------------
    def configure_node(self) -> BehaviorTree:
        """Apply the inventory settings onto the AutoInventoryHandler singleton at
        startup. All handler flags default to False, so this is required for the
        ID/salvage/deposit routines to act."""
        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            self.apply_to_handler()
            return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(name="ConfigureAutoInventory", action_fn=_fn, aftercast_ms=0)
        )

    def _deposit_glacial_gen(self):
        """Deposit ONLY Glacial Stones to Xunlai storage (the 'Deposit Glacial
        Stones' setting). The AutoInventoryHandler deposits by category only, so
        the per-model deposit is done here. Runs in town where storage is
        accessible."""
        if not self.settings.get("deposit_glacial_stones", False):
            return
        for _ in range(30):  # safety cap; materials stack, so usually 1-2 iterations
            if GLOBAL_CACHE.Inventory.GetModelCount(GLACIAL_STONE_MODEL) <= 0:
                return
            if not GLOBAL_CACHE.Inventory.DepositItemToStorageByModelID(GLACIAL_STONE_MODEL):
                return
            yield from Routines.Yield.wait(300)

    @staticmethod
    def _restock_to(model_id: int, desired: int, name: str) -> BehaviorTree:
        """Top up `model_id` to `desired` at the open merchant: buy
        (desired - current) when below, else do nothing. The quantity is computed
        when the node executes (at the merchant), so it reflects the live count."""
        def _build(_node: BehaviorTree.Node) -> BehaviorTree:
            need = desired - GLOBAL_CACHE.Inventory.GetModelCount(model_id)
            if need <= 0:
                return BehaviorTree(BehaviorTree.SucceederNode(name=f"{name}Skip"))
            return BT.BuyMerchantItem(model_id, quantity=need)

        return BehaviorTree(BehaviorTree.SubtreeNode(name=name, subtree_fn=_build))

    def _needs_merchant_node(self) -> BehaviorTree:
        """SUCCESS if there is anything to do at the merchant (buy ID/Salvage kits
        or sell accumulated junk), else FAILURE so the merchant walk is skipped.
        Cupcakes come from storage (not the merchant), so they don't force a walk."""
        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            inv = GLOBAL_CACHE.Inventory
            need_id = inv.GetModelCount(ModelID.Superior_Identification_Kit.value) < self._int("restock_id_kits")
            need_salv = inv.GetModelCount(ModelID.Salvage_Kit.value) < self._int("restock_salvage_kits")
            # Junk-to-sell proxy: we deposit everything we keep first, so a
            # filling inventory means sellable leftovers. Sell before the
            # low-inventory resign threshold rather than carrying junk run after run.
            need_sell = inv.GetFreeSlotCount() <= (self._int("free_inventory_slots") + 5)
            if need_id or need_salv or need_sell:
                _log("NeedsMerchant", f"visit (id={need_id} salv={need_salv} sell={need_sell})")
                return BehaviorTree.NodeState.SUCCESS
            _log("NeedsMerchant", "nothing to do at merchant -> skip the walk")
            return BehaviorTree.NodeState.FAILURE

        return BehaviorTree(
            BehaviorTree.ActionNode(name="NeedsMerchant", action_fn=_fn, aftercast_ms=0)
        )

    def _withdraw_kit_gold_node(self) -> BehaviorTree:
        """We deposit all gold, so before buying kits top the character up to
        KIT_WITHDRAW_GOLD from storage (only when a kit purchase is actually due)."""
        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            inv = GLOBAL_CACHE.Inventory
            buying = (inv.GetModelCount(ModelID.Superior_Identification_Kit.value) < self._int("restock_id_kits")
                      or inv.GetModelCount(ModelID.Salvage_Kit.value) < self._int("restock_salvage_kits"))
            if buying:
                need = KIT_WITHDRAW_GOLD - inv.GetGoldOnCharacter()
                avail = inv.GetGoldInStorage()
                if need > 0 and avail > 0:
                    inv.WithdrawGold(min(need, avail))
                    _log("WithdrawKitGold", f"withdrew up to {min(need, avail)}g to buy kits")
            return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(name="WithdrawKitGold", action_fn=_fn, aftercast_ms=300)
        )

    # --- keep-mods (never salvage/sell; deposit to storage in town) --------
    def _enabled_keep_upgrades(self) -> set:
        """The set of ItemUpgrade members the user has chosen to KEEP. Resolved
        lazily (so a library hiccup can't break module load) and returns an empty
        set when nothing is enabled or resolution fails -> the keep logic then
        no-ops and the inventory behaves exactly as before this feature."""
        if not self.keep_mod_names:
            return set()
        try:
            from Py4GWCoreLib.item_mods_src.types import ItemUpgrade
            return {getattr(ItemUpgrade, name) for name in self.keep_mod_names if hasattr(ItemUpgrade, name)}
        except Exception as exc:
            _log("KeepMods", f"could not resolve ItemUpgrade members: {exc!r}",
                 Py4GW.Console.MessageType.Error)
            return set()

    def _item_has_kept_mod(self, item, enabled: set) -> bool:
        """True if `item` (a bag snapshot item) carries any enabled keep-mod.
        Decodes the runtime modifiers with ItemModifierParser and matches each
        property's upgrade id against `enabled`. Inscriptions, suffixes and the
        inherent form of "of Devotion" all expose the same ItemUpgrade id, so a
        single `upgrade.id` check covers every encoding. Never raises."""
        try:
            mods = Item.Customization.Modifiers.GetModifiers(item.id)
            if not mods:
                return False
            from Py4GWCoreLib.item_mods_src.item_modifier_parser import ItemModifierParser
            for prop in ItemModifierParser(mods, item.rarity).get_properties():
                upgrade = getattr(prop, "upgrade", None)
                if upgrade is None or getattr(upgrade, "id", None) not in enabled:
                    continue
                # "Keep only max" gate: an enabled mod must also be at its maximum
                # value (Upgrade.is_maxed, a property). Fixed inscriptions report
                # maxed=True, so they always pass; only value-ranged mods (e.g. of
                # Devotion) get filtered. On any error determining max-ness, keep the
                # item rather than risk salvaging a good one.
                if self.keep_only_max:
                    try:
                        if not upgrade.is_maxed:
                            continue
                    except Exception:
                        pass
                return True
        except Exception:
            return False
        return False

    def _kept_mod_item_ids(self) -> set:
        """Ids of all bag items that should be kept (carry an enabled keep-mod).
        Recomputed on demand from a fresh bag snapshot; empty when no keep-mod is
        enabled. Items must be identified first for hidden mods to read (see the
        force-ID in apply_to_handler)."""
        enabled = self._enabled_keep_upgrades()
        if not enabled:
            return set()
        keep: set = set()
        try:
            for item in AutoInventoryHandler()._get_inventory_items():
                if self._item_has_kept_mod(item, enabled):
                    keep.add(item.id)
        except Exception as exc:
            _log("KeepMods", f"scan failed: {exc!r}", Py4GW.Console.MessageType.Error)
        return keep

    def _deposit_kept_mod_gen(self):
        """Deposit every kept-mod item in the bags to Xunlai storage. Runs in town
        (storage is only reachable in an outpost), honouring the user's wish to
        bank these items the next time in the outpost instead of salvaging them."""
        keep = self._kept_mod_item_ids()
        if not keep:
            return
        deposited = 0
        failed = 0
        for item_id in keep:
            if GLOBAL_CACHE.Inventory.DepositItemToStorage(item_id):
                deposited += 1
            else:
                failed += 1
            yield from Routines.Yield.wait(250)
        if deposited:
            _log("KeepMods", f"deposited {deposited} kept-mod item(s) to storage")
        if failed:
            _log("KeepMods",
                 f"{failed} kept-mod item(s) could NOT be deposited (storage full?) - left in bags",
                 Py4GW.Console.MessageType.Warning)

    def _salvage_target_ids(self, h, keep: set) -> list:
        """The bag item ids the handler WOULD salvage right now (rarity settings +
        salvageable + has at least one salvage mode), minus kept-mod items. Reuses
        the handler's OWN skip/mode logic so the per-item throttled pass salvages
        exactly the same set a bulk SalvageItems() call would - and the
        anti-disconnect delay then only falls on real salvages, not on skipped
        junk/kits (which would otherwise make the town pass needlessly slow)."""
        rarity_filter = h._normalize_rarity_names(None)  # empty -> honour salvage_* flags
        targets: list = []
        for item in h._get_inventory_items():
            if item.id in keep:
                continue
            if (h._get_salvage_skip_reason(item, rarity_filter) is None
                    and h._get_salvage_modes_for_item(item)):
                targets.append(item.id)
        return targets

    def _salvage_excluding_kept(self):
        """Salvage per the rarity settings, never touching a kept-mod item, ONE
        item per SalvageItems() call with a settle delay in between.

        Why one-at-a-time + delay: the disconnect the user hit during salvaging is
        a SERVER-side kick (game doesn't crash, reconnectable, nothing in the log),
        which GW issues when a salvage/kit action arrives before the client's
        inventory has re-synced with the server after the previous salvage - most
        visibly the moment a kit is consumed/destroyed. Salvaging one id at a time
        re-snapshots and re-picks the kit each call (a freshly emptied kit is never
        reused), and `salvage_throttle_ms` lets the inventory settle between
        actions so the next packet is valid. Tunable in the Inventory tab (0
        disables the delay). Falls back to a single bulk call if the precise
        pre-filter ever errors, so salvaging still happens."""
        h = AutoInventoryHandler()
        keep = self._kept_mod_item_ids()
        throttle = self._int("salvage_throttle_ms")
        try:
            targets = self._salvage_target_ids(h, keep)
        except Exception as exc:
            _log("Salvage", f"target pre-filter failed ({exc!r}); bulk-salvage fallback",
                 Py4GW.Console.MessageType.Warning)
            ids = [item.id for item in h._get_inventory_items() if item.id not in keep]
            yield from h.SalvageItems(item_ids=ids)
            return
        for item_id in targets:
            yield from h.SalvageItems(item_ids=[item_id])
            if throttle > 0:
                yield from Routines.Yield.wait(throttle)

    def _postkill_id_salvage_gen(self):
        """Explorable post-kill: identify, then salvage everything EXCEPT kept-mod
        items (no storage out here, so they stay in the bags until the town pass
        deposits them). Mirrors AutoInventoryHandler.IDAndSalvageItems with the
        keep-mod exclusion spliced into the salvage step."""
        h = AutoInventoryHandler()
        yield from h.IdentifyItems()
        yield from self._salvage_excluding_kept()
        yield

    def _town_id_salvage_deposit_gen(self):
        """Town pass: identify, deposit kept-mod items to storage, salvage the
        rest (kept ids still excluded as a safety net should a deposit fail), then
        the normal category deposit + gold deposit. Replaces the handler's bundled
        IDSalvageDepositItems with the keep-mod handling inserted between ID and
        salvage."""
        h = AutoInventoryHandler()
        yield from h.IdentifyItems()
        yield from self._deposit_kept_mod_gen()
        yield from self._salvage_excluding_kept()
        yield from h.DepositItemsAuto()
        yield from Routines.Yield.Items.DepositGold(h.keep_gold, log=False)

    def town_node(self) -> BehaviorTree:
        # Pass 1: identify+salvage+deposit (deposits all gold, keep_gold=0) +
        # deposit Glacial Stones + restock the cupcake from storage. Then walk to
        # the merchant ONLY if there is something to do there (NeedsMerchant):
        # withdraw kit gold, sell junk, restock kits. Pass 2 is a final
        # id/salvage/deposit (banks the gold from any sale). Merchant-dependent
        # nodes are Optional so a hiccup never kills the planner.
        ID = ModelID.Superior_Identification_Kit.value
        SALV = ModelID.Salvage_Kit.value
        id_qty = self._int("restock_id_kits")
        salv_qty = self._int("restock_salvage_kits")
        merchant = BehaviorTree(
            BehaviorTree.SequenceNode(
                name="Merchant",
                children=[
                    self._needs_merchant_node(),  # FAILURE here -> Optional below skips the walk
                    self._withdraw_kit_gold_node(),
                    BT.MoveAndInteract((MERCHANT_XY[0], MERCHANT_XY[1])),
                    BT.Wait(500),
                    Optional(BT.SellInventoryItems(exclude_models=_SELL_EXCLUDE), name="SellJunk"),
                    Optional(self._restock_to(ID, id_qty, "RestockIDKits"), name="RestockIDKitsOpt"),
                    Optional(self._restock_to(SALV, salv_qty, "RestockSalvageKits"), name="RestockSalvageKitsOpt"),
                ],
            )
        )
        return BehaviorTree(
            BehaviorTree.SequenceNode(
                name="Handle Inventory",
                children=[
                    _CoroutineNode("AutoIDSalvageDeposit#1", self._town_id_salvage_deposit_gen),
                    _CoroutineNode("DepositGlacialStones", self._deposit_glacial_gen),
                    # Cupcake restock is from storage (account-wide) - no merchant walk.
                    Optional(
                        BT.RestockItems(ModelID.Birthday_Cupcake.value, desired_quantity=1, allow_missing=True),
                        name="RestockCupcakeOpt",
                    ),
                    Optional(merchant, name="MerchantIfNeeded"),
                    _CoroutineNode("AutoIDSalvageDeposit#2", self._town_id_salvage_deposit_gen),
                ],
            )
        )

    def post_kill_node(self) -> BehaviorTree:
        # Explorable post-kill: identify + salvage only (no deposit/gold), but
        # never salvage kept-mod items - they ride along to town to be banked.
        return _CoroutineNode("PostKillIDSalvage", self._postkill_id_salvage_gen)

    def needs_management_node(self) -> BehaviorTree:
        """Resign when inventory is low on free slots or out of ID/Salvage kits.
        The resign defeats the party, which trips the same wipe detection
        DeathRecovery watches (IsPartyWiped/IsPartyDefeated) -> it returns to the
        outpost and restarts from STEP_TOWN, giving the town restock pass before
        farming resumes. (On a clean farm with inventory still OK this node is a
        no-op and the normal Reset Farm Loop keeps the cycle farm-only.)"""
        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            free_slots = GLOBAL_CACHE.Inventory.GetFreeSlotCount()
            id_kits = GLOBAL_CACHE.Inventory.GetModelCount(ModelID.Superior_Identification_Kit.value)
            salvage_kits = GLOBAL_CACHE.Inventory.GetModelCount(ModelID.Salvage_Kit.value)
            if free_slots < self._int("free_inventory_slots") or id_kits == 0 or salvage_kits == 0:
                _log("NeedsInventoryManagement",
                     f"resign: free={free_slots} id_kits={id_kits} salvage_kits={salvage_kits}",
                     Py4GW.Console.MessageType.Warning)
                Player.SendChatCommand("resign")
            return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(name="NeedsInventoryManagement", action_fn=_fn, aftercast_ms=500)
        )


INVENTORY = InventoryManager()
# endregion


# region loot (loop-until-clear; honors the Loot Manager via the LootConfig singleton)
def _normalize_model_id(mid):
    """Return a numeric model id or None. Accepts ints, ModelID members, and
    'ModelID.Foo' strings (mirror of LootManager._normalize_model_id)."""
    try:
        if isinstance(mid, int):
            return mid
        if isinstance(mid, ModelID):
            return mid.value
        if isinstance(mid, str):
            if mid.startswith("ModelID."):
                name = mid.split(".", 1)[1]
                if hasattr(ModelID, name):
                    return getattr(ModelID, name).value
            return None
        return int(mid)
    except Exception:
        return None


def _load_loot_manager_config(lc) -> None:
    """Populate the LootConfig singleton EXACTLY as the Loot Manager widget does,
    by reading the same two files the widget saves to:
        Widgets/Data/rarity_filter_data.json -> rarity flags
        Widgets/Config/loot_config.json       -> per-model whitelist/blacklist/dye
    Mirrors LootManager.load_rarity_filter_settings() + load_loot_config(), so the
    bot loots 'as specified in the Loot Manager' whether or not the widget is
    running (if it IS running it reloads the same files every ~2s and agrees -
    one source of truth, no clobbering).
    """
    base = Py4GW.Console.get_projects_path()
    rarity_file = os.path.join(base, "Widgets", "Data", "rarity_filter_data.json")
    config_file = os.path.join(base, "Widgets", "Config", "loot_config.json")

    # --- rarity flags ---
    rarity: dict = {}
    try:
        if os.path.exists(rarity_file):
            with open(rarity_file, "r") as f:
                rarity = json.load(f)
    except Exception as exc:
        _log("ConfigureLoot", f"failed to read rarity_filter_data.json: {exc!r}",
             Py4GW.Console.MessageType.Error)
    lc.SetProperties(
        loot_whites=rarity.get("white", False),
        loot_blues=rarity.get("blue", False),
        loot_purples=rarity.get("purple", False),
        loot_golds=rarity.get("gold", False),
        loot_greens=rarity.get("green", False),
        loot_gold_coins=rarity.get("gold_coins", False),
    )

    # --- per-model whitelist / blacklist / dye whitelist ---
    items: list = []
    blacklist: list = []
    dye_whitelist: list = []
    try:
        if os.path.exists(config_file):
            with open(config_file, "r") as f:
                data = json.load(f)
            if isinstance(data, list):          # legacy format: bare items list
                items = data
            else:
                items = data.get("items", [])
                blacklist = data.get("blacklist", [])
                dye_whitelist = data.get("dye_whitelist", [])
    except Exception as exc:
        _log("ConfigureLoot", f"failed to read loot_config.json: {exc!r}",
             Py4GW.Console.MessageType.Error)

    lc.ClearWhitelist()
    lc.ClearBlacklist()
    lc.ClearDyeWhitelist()

    for model_id in blacklist:
        norm = _normalize_model_id(model_id)
        if norm is not None:
            lc.AddToBlacklist(norm)
    for dye_id in dye_whitelist:
        lc.AddToDyeWhitelist(dye_id)

    for item in items:
        if not item.get("enabled", False):
            continue
        if item.get("group") == "Dyes":
            try:
                from Py4GWCoreLib import DyeColor
                dye_name = str(item.get("name", "")).replace(" Dye", "")
                lc.AddToDyeWhitelist(DyeColor[dye_name].value)
            except Exception:
                pass
            continue
        mid = _normalize_model_id(item.get("model_id"))
        if mid is not None:
            lc.AddToWhitelist(mid)

    # The manager honors gold_coins=True by whitelisting the coin model (the
    # rarity loop never consults loot_gold_coins). Mirror that exactly.
    if lc.loot_gold_coins:
        lc.AddToWhitelist(ModelID.Gold_Coins.value)

    if not os.path.exists(rarity_file) and not os.path.exists(config_file):
        _log("ConfigureLoot",
             "No Loot Manager config found (rarity_filter_data.json / loot_config.json "
             "missing) - nothing will be looted until you configure the Loot Manager.",
             Py4GW.Console.MessageType.Warning)


def ConfigureLoot() -> BehaviorTree:
    # Looting honors the Loot Manager exactly: load its saved config into the
    # process-wide LootConfig singleton (the same object the loot routine reads).
    # We no longer force all rarities on - that clobbered the user's filter and
    # filled bags with junk whites/blues, which then blocked wanted drops.
    def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
        _load_loot_manager_config(LootConfig())
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.ActionNode(name="ConfigureLoot", action_fn=_fn, aftercast_ms=0)
    )


def _loot_until_clear_gen():
    """Loop Routines.Yield.Items.LootItemsWithMaxAttempts until the filtered pile
    is gone, instead of a single BT.LootItems capped by one global timeout (which
    abandoned the rest of a large Vaettir pile when 12s elapsed).

    Each pass re-polls LootConfig().GetfilteredLootArray(LOOT_RANGE) - the SAME
    filter the Loot Manager configures - so late-registering drops and items that
    only came into range as we walked are collected. LootItemsWithMaxAttempts
    navmesh-paths to each item, retries, and skips-and-continues on an
    unreachable item (BT.LootItems instead re-selected the nearest item forever
    and burned the whole budget). Sets STATE.flags.in_looting so the SF caster
    stops rooting us mid-loot. Stops on: filtered array empty for N consecutive
    polls, player death, full inventory, or an overall safety timeout.
    """
    STATE.flags.in_looting = True
    try:
        start = Utils.GetBaseTimestamp()
        seen_loot = False
        clear_polls = 0
        skip_ids = set()   # agent ids LootItemsWithMaxAttempts could not reach this pass
        while True:
            # Overall safety cap, evaluated EVERY iteration (not only when the
            # filter is empty): a wanted-but-unreachable drop keeps the filter
            # non-empty forever, so the cap must be able to bound that path too.
            if Utils.GetBaseTimestamp() - start > LOOT_COMPLETION_TIMEOUT_MS:
                _log("LootUntilClear", "overall loot timeout reached",
                     Py4GW.Console.MessageType.Warning)
                return
            if Agent.IsDead(Player.GetAgentID()):
                return
            if GLOBAL_CACHE.Inventory.GetFreeSlotCount() <= 0:
                # Bags full: the post-kill ID/Salvage pass and the resign-on-low-
                # inventory check free space / route to town; don't spin here.
                _log("LootUntilClear", "inventory full -> ending loot pass",
                     Py4GW.Console.MessageType.Warning)
                return
            ids = [i for i in LootConfig().GetfilteredLootArray(LOOT_RANGE) if i not in skip_ids]
            if ids:
                seen_loot = True
                clear_polls = 0
                # LootItemsWithMaxAttempts returns the agent ids it could NOT pick
                # up (unreachable / bags full). Skip them on later polls so a
                # single unreachable drop can't keep the pile permanently
                # non-empty -> the normal empty-poll exit can then fire.
                failed = yield from Routines.Yield.Items.LootItemsWithMaxAttempts(
                    ids, log=False, pickup_timeout=3500, max_attempts=4,
                    attempts_timeout_seconds=2,
                )
                if failed:
                    skip_ids.update(failed)
                yield from Routines.Yield.wait(400)
                continue
            # Nothing (more) in range to loot.
            clear_polls += 1
            if seen_loot and clear_polls >= LOOT_CLEAR_POLLS_REQUIRED:
                return
            # Dry kill (no wanted drop ever appeared): don't idle the full cap.
            if not seen_loot and Utils.GetBaseTimestamp() - start > LOOT_FIRST_DROP_GRACE_MS:
                return
            yield from Routines.Yield.wait(400)
    finally:
        STATE.flags.in_looting = False


def LootUntilClear() -> BehaviorTree:
    return _CoroutineNode("LootUntilClear", _loot_until_clear_gen)
# endregion


# region custom combat / wait nodes
def WaitForBall(side_label: str, cycle_timeout_ms: int = 15000) -> BehaviorTree:
    """
    Wait until all earshot enemies have balled up within Adjacent range, or the
    timeout expires. Sets the in_waiting flag while active (so the stuck handler
    holds off) and casts Heart of Shadow on exit (builds that support an escape).
    """

    def _build(_node: BehaviorTree.Node) -> BehaviorTree:
        state: dict[str, object] = {"start": None}

        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            STATE.flags.in_waiting = True

            if state["start"] is None:
                state["start"] = Utils.GetBaseTimestamp()
                ConsoleLog(MODULE_NAME, f"Waiting for {side_label} to ball up.",
                           Py4GW.Console.MessageType.Info, False)

            if Agent.IsDead(Player.GetAgentID()):
                STATE.flags.in_waiting = False
                return BehaviorTree.NodeState.SUCCESS

            elapsed = Utils.GetBaseTimestamp() - int(state["start"])  # type: ignore[arg-type]
            if elapsed > cycle_timeout_ms:
                STATE.flags.in_waiting = False
                return BehaviorTree.NodeState.SUCCESS

            px, py = Player.GetXY()
            enemy_ids = Routines.Agents.GetFilteredEnemyArray(px, py, Range.Earshot.value)
            all_in_adjacent = True
            for enemy_id in enemy_ids:
                enemy = Agent.GetAgentByID(enemy_id)
                if enemy is None:
                    continue
                dx, dy = enemy.pos.x - px, enemy.pos.y - py
                if dx * dx + dy * dy > (Range.Adjacent.value ** 2):
                    all_in_adjacent = False
                    break

            if all_in_adjacent:
                STATE.flags.in_waiting = False
                return BehaviorTree.NodeState.SUCCESS

            return BehaviorTree.NodeState.RUNNING

        wait_node = BehaviorTree(
            BehaviorTree.ActionNode(name=f"WaitForBall_{side_label}", action_fn=_fn, aftercast_ms=0)
        )

        # On exit, push the ball away with Heart of Shadow (builds that support it).
        if CombatSignals.supports_escape():
            return BehaviorTree(
                BehaviorTree.SequenceNode(
                    name=f"WaitForBallAndHoS_{side_label}",
                    children=[
                        wait_node,
                        _CoroutineNode(
                            f"CastHoS_{side_label}",
                            lambda: CombatSignals.cast_escape(),
                        ),
                    ],
                )
            )
        return wait_node

    return BehaviorTree(BehaviorTree.SubtreeNode(name=f"WaitForBall_{side_label}_root", subtree_fn=_build))


def FollowAndWaitForBall(path: List[Tuple[float, float]], side_label: str,
                         cycle_timeout_ms: int = 15000) -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name=f"FollowAndWait_{side_label}",
            children=[
                BT.MoveDirect(path),
                WaitForBall(side_label, cycle_timeout_ms),
            ],
        )
    )


def KillEnemies(timeout_ms: int = 120000) -> BehaviorTree:
    """
    Signal the build it is in the killing routine and wait until all spellcast
    enemies are dead. Resign on timeout; bail on death.
    """

    def _build(_node: BehaviorTree.Node) -> BehaviorTree:
        state: dict[str, object] = {"start": None}

        def _fn(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            if state["start"] is None:
                state["start"] = Utils.GetBaseTimestamp()
                STATE.flags.in_killing = True
                CombatSignals.set_killing(True)

            if Agent.IsDead(Player.GetAgentID()):
                STATE.flags.in_killing = False
                CombatSignals.set_killing(False)
                return BehaviorTree.NodeState.SUCCESS

            elapsed = Utils.GetBaseTimestamp() - int(state["start"])  # type: ignore[arg-type]
            if elapsed > timeout_ms:
                _log("KillEnemies", f"timeout reached elapsed_ms={elapsed}, resigning",
                     Py4GW.Console.MessageType.Error)
                Player.SendChatCommand("resign")
                STATE.flags.in_killing = False
                CombatSignals.set_killing(False)
                return BehaviorTree.NodeState.SUCCESS

            px, py = Player.GetXY()
            enemies = Routines.Agents.GetFilteredEnemyArray(px, py, Range.Spellcast.value)
            if len(enemies) > 0:
                return BehaviorTree.NodeState.RUNNING

            # cleared
            STATE.flags.in_killing = False
            STATE.flags.finished = True
            CombatSignals.set_killing(False)
            CombatSignals.set_finished(True)
            ConsoleLog(MODULE_NAME, "Finished killing routine.", Py4GW.Console.MessageType.Info, True)
            return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(name="KillEnemiesAction", action_fn=_fn, aftercast_ms=0)
        )

    return BehaviorTree(BehaviorTree.SubtreeNode(name="KillEnemies", subtree_fn=_build))
# endregion


# region stuck handler service tree
def _handle_stuck_fn(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
    """Persistent port of legacy HandleStuckJagaMoraine. Ticked once/frame; all
    cross-tick state lives in node.blackboard (closures reset on tree reset)."""
    bb = node.blackboard
    if "yavb_stuck_init" not in bb:
        bb["yavb_stuck_init"] = True
        bb["yavb_stuck_counter"] = 0
        bb["yavb_old_pos"] = Player.GetXY()
        bb["yavb_stuck_timer"] = ThrottledTimer(5000)   # scheduled /stuck
        bb["yavb_move_timer"] = ThrottledTimer(3000)    # movement stall check
        bb["yavb_resigned"] = False                     # resign latch (once/instance)

    def _clear() -> None:
        bb["yavb_stuck_counter"] = 0
        CombatSignals.set_stuck(0)

    def _resync() -> None:
        bb["yavb_old_pos"] = Player.GetXY()
        bb["yavb_stuck_timer"].Reset()
        bb["yavb_move_timer"].Reset()

    # Stuck handling is scoped to the Jaga Moraine farm instance ONLY. In the
    # outpost / Bjora traverse / loading / while dead we do nothing (the legacy
    # bot only ran this coroutine during the farm). This also stops the outpost's
    # large instance uptime from falsely tripping the 7-minute watchdog.
    if (not Routines.Checks.Map.MapValid()
            or Map.GetMapID() != JAGA_MORAINE
            or Agent.IsDead(Player.GetAgentID())):
        _clear()
        _resync()
        bb["yavb_resigned"] = False
        return BehaviorTree.NodeState.RUNNING

    # 7-minute instance watchdog (resign ONCE per instance, not every tick).
    if Map.GetInstanceUptime() / 1000 > 7 * 60:
        if not bb["yavb_resigned"]:
            _log("HandleStuck", "Instance watchdog -> resign", Py4GW.Console.MessageType.Warning)
            _clear()
            Player.SendChatCommand("resign")
            bb["yavb_resigned"] = True
        return BehaviorTree.NodeState.RUNNING

    if STATE.flags.in_waiting or STATE.flags.finished or STATE.flags.in_killing or STATE.flags.in_looting:
        _clear()
        bb["yavb_stuck_timer"].Reset()
        return BehaviorTree.NodeState.RUNNING

    if bb["yavb_stuck_timer"].IsExpired():
        Player.SendChatCommand("stuck")
        bb["yavb_stuck_timer"].Reset()

    if bb["yavb_move_timer"].IsExpired():
        cur = Player.GetXY()
        # The BT mover pauses UNCONDITIONALLY while the player is casting
        # (movement.py _get_pause_reason -> "casting"), and the SF build runs
        # continuous SF/Shroud/Channeling/WoP upkeep (heaviest near enemies). A
        # casting-rooted player has an unchanged position but is NOT stuck.
        # Counting it would set stuck_signal -> the build casts Heart of Shadow
        # -> that cast re-roots the mover -> position frozen again -> HoS spam +
        # no path progress. Only treat an unchanged position as stuck when the
        # player is genuinely idle (NOT casting).
        if Agent.IsCasting(Player.GetAgentID()):
            bb["yavb_old_pos"] = cur   # rebaseline; never accuse a caster
            _clear()                   # drop any stale stuck_signal
        elif cur == bb["yavb_old_pos"]:
            # Genuinely idle and not moving -> real (e.g. body-block) stuck.
            # Preserve the intended Heart-of-Shadow shadow-step escape.
            Player.SendChatCommand("stuck")
            bb["yavb_stuck_counter"] += 1
            CombatSignals.set_stuck(bb["yavb_stuck_counter"])
            bb["yavb_stuck_timer"].Reset()
        else:
            bb["yavb_old_pos"] = cur
            _clear()                   # moved -> also clear the build's stuck_signal
        bb["yavb_move_timer"].Reset()

    if bb["yavb_stuck_counter"] >= 10 and not bb["yavb_resigned"]:
        _log("HandleStuck", "Unrecoverable stuck -> resign", Py4GW.Console.MessageType.Error)
        _clear()
        Player.SendChatCommand("resign")
        bb["yavb_resigned"] = True
    return BehaviorTree.NodeState.RUNNING


def HandleStuck() -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.ActionNode(name="HandleStuck", action_fn=_handle_stuck_fn, aftercast_ms=0)
    )
# endregion


# region death recovery service tree (faithful port of legacy on_death routing)
def _death_recovery_fn(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
    """Persistent death / party-wipe recovery.

    Faithful port of the legacy `on_death` callback, which jumped the FSM to
    "[H]Town Routines_1" on death. On a wipe we return to the outpost and then
    restart the planner from STEP_TOWN (full reset: travel to Longeyes Ledge,
    restock, re-traverse Bjora, re-enter Jaga). This replaces BottingTree's
    built-in PartyWipeRecoveryService, which would restart from the *current*
    step (STEP_FARM) and therefore resume the farm pathing while standing in the
    wrong map after death.

    Trigger condition is the SAME one the planner uses to freeze itself
    (ticks.py `_tick_planner`: IsPartyWiped() or IsPartyDefeated()), so while
    this recovery is active the planner is already paused and the farm sequence
    cannot advance underneath us. Cross-tick state lives in node.blackboard
    because BottingTree.Reset() clears the blackboard on every restart (the
    `not in bb` guard re-inits us cleanly each run).
    """
    bb = node.blackboard
    if "yavb_death_init" not in bb:
        bb["yavb_death_init"] = True
        bb["yavb_death_active"] = False
        bb["yavb_death_return_timer"] = ThrottledTimer(1000)  # throttle ReturnToOutpost spam

    is_wiped = bool(
        Routines.Checks.Party.IsPartyWiped()
        or GLOBAL_CACHE.Party.IsPartyDefeated()
    )

    if not bb["yavb_death_active"]:
        if not is_wiped:
            return BehaviorTree.NodeState.RUNNING
        # Latch recovery and drop any queued actions so we stop farming. Clear
        # the looting latch too: if we died mid-loot the loot generator is
        # abandoned without running its finally, and a stuck in_looting flag
        # would keep the SF caster gated off through the post-restart Bjora
        # traverse (which needs SF upkeep).
        STATE.flags.in_looting = False
        bb["yavb_death_active"] = True
        bb["yavb_death_return_timer"].Reset()
        ActionQueueManager().ResetAllQueues()
        _log("DeathRecovery", "Party wipe detected -> returning to outpost",
             Py4GW.Console.MessageType.Warning)
        return BehaviorTree.NodeState.RUNNING

    # Latched: once we are safely back in a loaded outpost, request the full
    # Town Routines reset and clear the latch.
    if Map.IsMapReady() and Map.IsOutpost() and GLOBAL_CACHE.Party.IsPartyLoaded():
        bb["restart_step_name_request"] = STEP_TOWN
        bb["yavb_death_active"] = False
        _log("DeathRecovery", f"Back in outpost -> restart from '{STEP_TOWN}'",
             Py4GW.Console.MessageType.Warning)
        return BehaviorTree.NodeState.RUNNING

    # Not in an outpost yet: keep asking the party to return.
    if bb["yavb_death_return_timer"].IsExpired():
        GLOBAL_CACHE.Party.ReturnToOutpost()
        bb["yavb_death_return_timer"].Reset()
    return BehaviorTree.NodeState.RUNNING


def DeathRecovery() -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.ActionNode(name="DeathRecovery", action_fn=_death_recovery_fn, aftercast_ms=0)
    )
# endregion


# region planner steps
def InitializeBot() -> BehaviorTree:
    bot = ensure_botting_tree()
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Initialize Bot",
            children=[
                BT.ResetActionQueues(),
                # Headless HeroAI OFF - our SFBuildCaster service drives combat.
                bot.Config.Pacifist(auto_loot=False, pause_on_danger=False),
                # Configure the AutoInventoryHandler singleton (id/salvage/deposit
                # flags) in code; we drive it manually from the inventory nodes.
                INVENTORY.configure_node(),
                # Load the Loot Manager config into the LootConfig singleton so
                # LootUntilClear loots exactly what the user configured (the
                # singleton defaults to all-off, so this is required).
                ConfigureLoot(),
            ],
        )
    )


def TownRoutines() -> BehaviorTree:
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Town Routines",
            children=[
                BT.Travel(target_map_id=LONGEYES_LEDGE),
                EquipSkillBar(),
                INVENTORY.town_node(),
                BT.SetHardMode(True),
                BT.MoveAndExitMap((-26375, 16180), target_map_id=BJORA_MARCHES),
            ],
        )
    )


def TraverseBjoraMarches() -> BehaviorTree:
    path: List[Tuple[float, float]] = [
        (17810, -17649), (17516, -17270), (17166, -16813), (16862, -16324), (16472, -15934),
        (15929, -15731), (15387, -15521), (14849, -15312), (14311, -15101), (13776, -14882),
        (13249, -14642), (12729, -14386), (12235, -14086), (11748, -13776), (11274, -13450),
        (10839, -13065), (10572, -12590), (10412, -12036), (10238, -11485), (10125, -10918),
        (10029, -10348), (9909, -9778), (9599, -9327), (9121, -9009), (8674, -8645),
        (8215, -8289), (7755, -7945), (7339, -7542), (6962, -7103), (6587, -6666),
        (6210, -6226), (5834, -5788), (5457, -5349), (5081, -4911), (4703, -4470),
        (4379, -3990), (4063, -3507), (3773, -3031), (3452, -2540), (3117, -2070),
        (2678, -1703), (2115, -1593), (1541, -1614), (960, -1563), (388, -1491),
        (-187, -1419), (-770, -1426), (-1343, -1440), (-1922, -1455), (-2496, -1472),
        (-3073, -1535), (-3650, -1607), (-4214, -1712), (-4784, -1759), (-5278, -1492),
        (-5754, -1164), (-6200, -796), (-6632, -419), (-7192, -300), (-7770, -306),
        (-8352, -286), (-8932, -258), (-9504, -226), (-10086, -201), (-10665, -215),
        (-11247, -242), (-11826, -262), (-12400, -247), (-12979, -216), (-13529, -53),
        (-13944, 341), (-14358, 743), (-14727, 1181), (-15109, 1620), (-15539, 2010),
        (-15963, 2380), (-18048, 4223), (-19196, 4986), (-20000, 5595), (-20300, 5600),
    ]
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Traverse Bjora Marches",
            children=[
                RoutinesBT.Player.SetTitle(TitleID.Norn.value),
                BT.MoveDirect(path),
                BT.WaitForMapLoad(map_id=JAGA_MORAINE),
            ],
        )
    )


def JagaMoraineFarm() -> BehaviorTree:
    inner_packs = [
        (13367, -20771), (11375, -22761), (10925, -23466), (10917, -24311), (10280, -24620),
        (10280, -24620), (9640, -23175), (7815, -23200), (6626.51, -23167.24),
    ]
    left_ball = [(7765, -22940), (8213, -22829), (8740, -22475), (8880, -21384), (8684, -20833), (8982, -20576)]
    log_side = [(10196, -20124), (10123, -19529), (10049, -18933)]
    big_pack = [(9976, -18338), (11316, -18056), (10392, -17512), (10114, -16948)]
    right_ball = [
        (10729, -16273), (10505, -14750), (10815, -14790), (11090, -15345), (11670, -15457),
        (12604, -15320), (12450, -14800), (12725, -14850), (12476, -16157),
    ]
    to_kill_spot = [
        (13070, -16911), (12938, -17081), (12790, -17201), (12747, -17220), (12703, -17239),
        (12684, -17184), (12485.18, -17260.41),
    ]

    def _reset_flags(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
        STATE.flags.reset()
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Jaga Moraine Farm",
            children=[
                BehaviorTree.ActionNode(name="ResetFarmFlags", action_fn=_reset_flags, aftercast_ms=0),
                AssignBuild(),
                BT.Move((13372.44, -20758.50)),
                BT.TargetNearestAndAutoDialog((13367, -20771), buttons=0, target_distance=200.0),
                FollowAndWaitForBall(inner_packs, "Inner Packs", cycle_timeout_ms=7500),
                FollowAndWaitForBall(left_ball, "Left Aggro Ball"),
                FollowAndWaitForBall(log_side, "Log Side Packs", cycle_timeout_ms=7500),
                FollowAndWaitForBall(big_pack, "Big Pack"),
                FollowAndWaitForBall(right_ball, "Right Aggro Ball"),
                BT.MoveDirect(to_kill_spot),
                KillEnemies(),
                # Let drops register as item agents before looting; a bare loot
                # call right after the kill sees an empty array and loots nothing.
                BT.Wait(800),
                # Loop-until-clear loot pass: honors the Loot Manager and
                # navmesh-walks to every drop (replaces the single 12s-capped
                # BT.LootItems that abandoned big piles).
                LootUntilClear(),
                # Explorable post-kill: identify + salvage only (no deposit/gold).
                INVENTORY.post_kill_node(),
                INVENTORY.needs_management_node(),
                BT.MoveAndExitMap((15850, -20550), target_map_id=BJORA_MARCHES),
            ],
        )
    )


def RequestRestart(step_name: str) -> BehaviorTree:
    """Set the planner restart lever. After the current tick, BottingTree.tick()
    -> ProcessRestartRequest() rebuilds the planner starting from `step_name`
    and re-Start()s it (see planner.py). This is how we loop without the
    `repeat=True` full-list repeater, so the loop is scoped to a single leg."""

    def _fn(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        node.blackboard["restart_step_name_request"] = step_name
        _log("RequestRestart", f"-> restart from '{step_name}'")
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.ActionNode(name=f"RequestRestart({step_name})", action_fn=_fn, aftercast_ms=0)
    )


def ResetFarmLoop() -> BehaviorTree:
    """Farm-only loop leg (port of legacy ResetFarmLoop + JumpToStepName).

    The farm step exits Jaga -> Bjora at (15850, -20550). Here we hop straight
    back Bjora -> Jaga via the traverse-end portal at (-20300, 5600) and request
    a restart from STEP_FARM, so the normal cycle re-runs ONLY the farm leg -
    never the town/restock/long-traverse legs. Those run only on first start and
    after death (DeathRecovery -> STEP_TOWN)."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Reset Farm Loop",
            children=[
                BT.MoveAndExitMap((-20300, 5600), target_map_id=JAGA_MORAINE),
                RequestRestart(STEP_FARM),
            ],
        )
    )
# endregion


# region wiring
def get_execution_steps() -> list[tuple[str, Callable[[], BehaviorTree]]]:
    return [
        (STEP_INITIALIZE, InitializeBot),
        (STEP_TOWN, TownRoutines),
        (STEP_TRAVERSE, TraverseBjoraMarches),
        (STEP_FARM, JagaMoraineFarm),
        (STEP_RESET_LOOP, ResetFarmLoop),
    ]


def ensure_botting_tree() -> BottingTree:
    if STATE.tree is None:
        tree = BottingTree.Create(
            MODULE_NAME,
            main_routine=get_execution_steps(),
            routine_name="YAVB Vaettir Sequence",
            # repeat=False: we do NOT use the built-in full-list repeater (which
            # would re-run town + long traverse every cycle). Instead the
            # Reset Farm Loop step restarts from STEP_FARM for a faithful
            # farm-only loop, and DeathRecovery restarts from STEP_TOWN on a wipe.
            repeat=False,
            reset=False,
            multi_account=False,
            configure_fn=lambda tree: tree.Config.ConfigureUpkeep(
                looting_enabled=False,
                # Disable the built-in party-wipe recovery: it resumes the CURRENT
                # step, which after a farm death would re-run the farm pathing in
                # the wrong map. Our DeathRecovery service routes to STEP_TOWN
                # instead. (SetMainRoutine still re-adds the built-in service, so
                # we also strip it by name below.)
                enable_party_wipe_recovery=False,
                # Turn OFF the standalone "HeroAI" widget for the whole run
                # (restored on Stop). Headless HeroAI (inside the tree) is already
                # off via Pacifist, but BottingTree only auto-disables the HeroAI
                # WIDGET when headless is ON - so on this Pacifist, single-account
                # bot the widget would otherwise keep running and drive the SAME
                # skillbar as our SF caster: that double-driving caused the
                # auto-attacks during the Bjora traverse and the Heart of Shadow
                # spam (HeroAI uses HoS in its rotation). Our SF caster must be the
                # ONLY combat driver. This is the BottingTree equivalent of the
                # legacy bot.Properties.Disable("hero_ai").
                deactivate_widget_list=["HeroAI"],
                # Headless HeroAI is intentionally off (our SF caster handles
                # combat); silence the per-tick "Headless HeroAI is disabled" log.
                heroai_state_logging=False,
                # We drive AutoInventoryHandler from the planner (town / post-kill);
                # keep the InventoryPlus widget from double-driving it.
                auto_inventory_handler_enabled=False,
            ),
        )
        # SetMainRoutine() unconditionally (re)adds 'PartyWipeRecoveryService'
        # (planner.py EnsurePartyWipeRecoveryService). Strip it so only our
        # STEP_TOWN-routing DeathRecovery runs. No public RemoveServiceTree
        # exists, so filter _service_steps and rebuild via SetServiceTrees.
        tree.SetServiceTrees([
            (name, builder)
            for name, builder in tree._service_steps
            if name != "PartyWipeRecoveryService"
        ])
        # Combat + stuck handling + death recovery run as persistent service
        # trees beside the planner.
        tree.AddUpkeepTree("SFBuildCaster", SFBuildCaster)
        tree.AddUpkeepTree("HandleStuck", HandleStuck)
        tree.AddUpkeepTree("DeathRecovery", DeathRecovery)
        STATE.tree = tree
    return STATE.tree


def main() -> None:
    if not STATE.initialized:
        if not STATE.ini_key:
            STATE.ini_key = IniManager().ensure_key(INI_PATH, INI_FILENAME)
            if not STATE.ini_key:
                return
            IniManager().load_once(STATE.ini_key)
        ensure_botting_tree()
        STATE.initialized = True

    # Inventory settings load from a self-contained JSON file (no account /
    # DB-threading constraints), so this succeeds on the first frame.
    if not STATE.inv_loaded:
        INVENTORY.load()
        STATE.inv_loaded = True
        INVENTORY.apply_to_handler()
        _log("InvLoad",
             f"settings loaded: {INVENTORY.settings} ints={INVENTORY.int_settings} "
             f"keep={INVENTORY.keep_mod_names}")

    tree = ensure_botting_tree()
    # YAVB3's SFBuildCaster is the SOLE combat driver, so headless HeroAI must
    # stay OFF - if on it double-drives the loaded skillbar (Heart of Shadow spam
    # / no movement on aggro). The BottingTree defaults the headless flag ON and
    # exposes it as a Settings-tab checkbox; Config.Pacifist only flips it off
    # when InitializeBot runs (the farm-only loop restart skips that step, and the
    # checkbox can be toggled by hand). Force it off every frame so it cannot
    # drift back on.
    if tree.IsHeadlessHeroAIEnabled():
        tree.SetHeadlessHeroAIEnabled(False, reset_runtime=False)
    # Same for the BottingTree built-in "Looting" upkeep (its own Settings-tab
    # checkbox): when active it sets LOOTING_ACTIVE -> the planner reads that as
    # PAUSED_ON_LOOTING and FREEZES the planner, and PAUSE_MOVEMENT roots the
    # player. Our LootUntilClear loot pass lives INSIDE the planner, so an active
    # built-in looter would stall our pass and double-loot against our navmesh
    # loot walk. We loot ourselves, so keep built-in looting OFF every frame.
    if tree.IsLootingEnabled():
        tree.SetLootingEnabled(False)
    tree.tick()
    tree.UI.draw_window(extra_tabs=[
        ("Inventory", INVENTORY.draw_tab),
        ("Mods", INVENTORY.draw_mods_tab),
    ])


if __name__ == "__main__":
    main()
# endregion
