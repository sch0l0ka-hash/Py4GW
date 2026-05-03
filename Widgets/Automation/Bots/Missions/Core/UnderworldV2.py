# ╔══════════════════════════════════════════════════════════════════════════════
# ║  File    : UnderworldV2.py
# ║  Purpose : Fully automated Guild Wars Underworld bot (rebuild).
# ║            Built on the BottingTree framework with named planner steps,
# ║            one step per UW quest section.
# ╚══════════════════════════════════════════════════════════════════════════════

from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.IniManager import IniManager
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree

# Force a fresh reimport of BottingTreeFunctions on every script (re)load so
# edits to that helper module are picked up without restarting Py4GW.
import sys as _sys
for _mod_key in [k for k in list(_sys.modules) if k.endswith("BottingTreeFunctions")]:
    del _sys.modules[_mod_key]
del _sys

from Widgets.Automation.Bots.Missions.Core import BottingTreeFunctions as BTF
from Sources.ApoSource.ApoBottingLib import wrappers as BT

import os
import Py4GW
import PyImGui
from Py4GWCoreLib import name_to_map_id
from Py4GWCoreLib import AgentArray, Agent, Player
from Py4GWCoreLib import GLOBAL_CACHE
from Py4GWCoreLib.Quest import Quest
from Py4GWCoreLib.native_src.internals.types import Vec2f


# ── Module identity ──────────────────────────────────────────────────────────
MODULE_NAME = "UnderworldV2"
MODULE_ICON = os.path.join(Py4GW.Console.get_projects_path(), "Textures", "Module_Icons", "Underworld.png")
BOT_NAME    = "Underworld V2"

# ── Persistent configuration (INI file) ──────────────────────────────────────
INI_PATH = "Widgets/Config"
INI_FILENAME = "UnderworldV2.ini"

# ── Module state ─────────────────────────────────────────────────────────────
initialized = False
INI_KEY = ""
botting_tree: BottingTree | None = None


# ╔══════════════════════════════════════════════════════════════════
# ║                       BOT CONFIGURATION
# ╚══════════════════════════════════════════════════════════════════

# Widgets that must run on every account during an UW run.
REQUIRED_WIDGETS: tuple[str, ...] = (
    "MerchantRules",
    "HeroAI",
    "Dhuum Helper",
    "Return to outpost on defeat",
)

# Outposts from which the leader can scroll into the Underworld.
# Mapping: ini_key -> (display_name, map_id)
UW_ENTRYPOINTS: dict[str, tuple[str, int]] = {
    "embark_beach":       ("Embark Beach",       int(name_to_map_id["Embark Beach"])),
    "temple_of_the_ages": ("Temple of the Ages", int(name_to_map_id["Temple of the Ages"])),
    "chantry_of_secrets": ("Chantry of Secrets", int(name_to_map_id["Chantry of Secrets"])),
    "zin_ku_corridor":    ("Zin Ku Corridor",    int(name_to_map_id["Zin Ku Corridor"])),
}
DEFAULT_UW_ENTRYPOINT_KEY = "embark_beach"

# Underworld map id + Passage Scroll to the Underworld model id.
UW_MAP_ID = 72
UW_SCROLL_MODEL_ID = 3746

# UW NPC model ids (subset; matches legacy underworld.py UWNpcModelID).
LOST_SOUL_MODEL_ID = 2425
REAPER_OF_THE_LABYRINTH_MODEL_ID = 2399
# All UW Reapers share the same model id; aliases kept for readability.
REAPER_OF_THE_MOUNTAINS_MODEL_ID = 2399
REAPER_OF_THE_CHAOS_PLANES_MODEL_ID = 2399

# Mindblade Spectre model id (Chaos Planes spawn that floods the area
# until pulled & cleared).
MINDBLADE_MODEL_ID = 2380

# Chamber clearing path: leader walks this loop after taking the quest to
# pull and clear the entry chamber spawns.
CHAMBER_PATH: list[Vec2f] = [
    Vec2f(-1536.0,  6068.0),
    Vec2f(-1264.0,  8556.0),
    Vec2f(  468.0,  9151.0),
    Vec2f( 1199.0, 10204.0),
    Vec2f( 1097.0, 12548.0),
    Vec2f( -933.0, 13312.0),
    Vec2f(-3905.0, 13295.0),
    Vec2f(-4653.0, 11665.0),
    Vec2f(-6360.0, 10255.0),
    Vec2f(-5740.0, 12752.0),
]

# Restore-the-Mountains path: leader walks the perimeter loop after taking
# the "Restoring Grenth's Monuments" quest to clear the Mountains spawns.
RESTORE_MOUNTAINS_PATH: list[Vec2f] = [
    Vec2f(-4752.0,  11799.0),
    Vec2f(-2810.0,  10217.0),
    Vec2f(-1653.0,  10567.0),
    Vec2f( -825.0,   8954.0),
    Vec2f(-1270.0,   6565.0),
    Vec2f(-3054.0,   4622.0),
    Vec2f(-3287.0,   2297.0),
    Vec2f(-2236.0,   1703.0),
    Vec2f(  152.0,   3338.0),
    Vec2f(  396.0,   1480.0),
    Vec2f( 2392.0,   2987.0),
    Vec2f( 4897.0,   1724.0),
    Vec2f( 7902.0,    981.0),
    Vec2f( 8602.0,  -1715.0),
    Vec2f( 8071.0,  -4210.0),
    Vec2f( 7509.0,  -6259.0),
    Vec2f( 6129.0,  -7766.0),
    Vec2f( 2923.0,  -7766.0),
    Vec2f( 2180.0, -10516.0),
    Vec2f(-1048.0,  -8743.0),
    Vec2f(-3532.0,  -6492.0),
    Vec2f(-5676.0,  -4422.0),
    Vec2f(-8213.0,  -5059.0),
]

# Demon Assassin approach: leader walks to the pull spot after taking the
# quest from the Reaper of the Twin Serpent Mountains.
DEMON_ASSASSIN_PATH: list[Vec2f] = [
    Vec2f(-3489.0, -5852.0),
]

# Restore-the-Chaos-Planes path: leader walks the loop after taking the
# "Restoring Grenth's Monuments" quest leg in the Chaos Planes to clear
# the Chaos Planes spawns.
RESTORE_CHAOS_PLANES_PATH: list[Vec2f] = [
    Vec2f(  -937.0,  -8765.0),
    Vec2f(  2413.0, -10473.0),
    Vec2f(  6875.0,  -7703.0),
    Vec2f(  9679.0,  -9926.0),
    Vec2f( 12299.0,  -8334.0),
    Vec2f( 13577.0, -10921.0),
    Vec2f( 13842.0, -15416.0),
]

# Hold spot used while waiting for Mindblade Spectres to despawn at the end
# of the Chaos Planes path. The leader is anchored here so the team does
# not wander into the spawn.
CHAOS_PLANES_MINDBLADE_HOLD_POSITION: Vec2f = Vec2f(13784.0, -15512.0)

# Second Mindblade hold spot: after the first wave despawns, the leader
# moves here and waits for the next set of Mindblades to clear too.
CHAOS_PLANES_MINDBLADE_HOLD_POSITION_2: Vec2f = Vec2f(11052.0, -17990.0)

# Four Horsemen positions / paths.
# Pre-position used at the start of the quest leg (still in the Mountains
# area, before talking to the Reaper of the Chaos Planes).
FOUR_HORSEMEN_PRE_POSITION: Vec2f = Vec2f(13473.0, -12091.0)
# Reaper of the Chaos Planes location (quest giver / TP-back-to-Lab dialog).
FOUR_HORSEMEN_NPC_POSITION: Vec2f = Vec2f(11371.0, -17990.0)
# Hold spot where the team anchors while the Horsemen are pulled & killed.
FOUR_HORSEMEN_HOLD_POSITION: Vec2f = Vec2f(11510.0, -18234.0)
# Position used after TP-to-Lab to walk back to the Reaper of the Labyrinth
# and trigger the "Tp back to Chaos" dialog.
FOUR_HORSEMEN_LAB_RETURN_POSITION: Vec2f = Vec2f(-5782.0, 12819.0)

# Hero/follower flag spread used at the pre-position before taking the
# Four Horsemen quest. All 7 heroes are stacked onto the same spot so the
# team stays together while HeroAI / followers settle.
FOUR_HORSEMEN_PRE_FLAG_POINTS: list[Vec2f] = [
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
    Vec2f(13621.0, -11797.0),
]

# Hero/follower flag spread used during the Horsemen hold. All 7 heroes
# are stacked onto the same hold spot so the team stays together while
# waiting for the quest to complete.
FOUR_HORSEMEN_HOLD_FLAG_POINTS: list[Vec2f] = [
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
    Vec2f(11232.0, -18241.0),
]


# ╔══════════════════════════════════════════════════════════════════
# ║                       PLANNER STEP HELPERS
# ╚══════════════════════════════════════════════════════════════════

def _todo_step(step_name: str) -> BehaviorTree:
    """Placeholder planner step. Logs once and returns SUCCESS so the
    sequence advances to the next step. Replace with real logic later."""

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        node.blackboard[f"{step_name}_NOTE"] = f"TODO: implement {step_name}"
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        root=BehaviorTree.ActionNode(
            name=f"{step_name}Tick",
            action_fn=_tick,
        )
    )


def _wait_until_active_quest_completed(
    name: str = "WaitUntilActiveQuestCompleted",
    timeout_ms: int = 0,
    throttle_interval_ms: int = 500,
) -> BehaviorTree:
    """Wait until the currently active quest is flagged as completed in the
    quest log. Mirrors the legacy underworld.py check
    ``Quest.GetActiveQuest() > 0 and Quest.IsQuestCompleted(...)``.
    Returns FAILURE only if the optional ``timeout_ms`` is exceeded."""

    def _condition() -> BehaviorTree.NodeState:
        active = int(Quest.GetActiveQuest())
        if active > 0 and bool(Quest.IsQuestCompleted(active)):
            return BehaviorTree.NodeState.SUCCESS
        # Keep waiting; returning False would be interpreted as FAILURE.
        return BehaviorTree.NodeState.RUNNING

    return BehaviorTree(
        root=BehaviorTree.WaitUntilNode(
            condition_fn=_condition,
            throttle_interval_ms=throttle_interval_ms,
            timeout_ms=timeout_ms,
            name=name,
        )
    )


def _blacklist_enemy_name(enemy_name: str, *, name: str | None = None) -> BehaviorTree:
    """Add ``enemy_name`` to the global EnemyBlacklist (case-insensitive).
    Always returns SUCCESS so the sequence keeps advancing."""
    from Py4GWCoreLib.EnemyBlacklist import EnemyBlacklist

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        EnemyBlacklist().add_name(enemy_name)
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        root=BehaviorTree.ActionNode(
            name=name or f"Blacklist '{enemy_name}'",
            action_fn=_tick,
        )
    )


def _unblacklist_enemy_name(enemy_name: str, *, name: str | None = None) -> BehaviorTree:
    """Remove ``enemy_name`` from the global EnemyBlacklist (case-insensitive).
    Always returns SUCCESS so the sequence keeps advancing."""
    from Py4GWCoreLib.EnemyBlacklist import EnemyBlacklist

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        EnemyBlacklist().remove_name(enemy_name)
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        root=BehaviorTree.ActionNode(
            name=name or f"Unblacklist '{enemy_name}'",
            action_fn=_tick,
        )
    )


def _wait_until_no_alive_model(
    model_id: int,
    *,
    name: str = "WaitUntilNoAliveModel",
    timeout_ms: int = 20_000,
    throttle_interval_ms: int = 500,
    hold_position: Vec2f | None = None,
    hold_tolerance: float = 150.0,
    waves: int = 4,
    delay_between_waves_ms: int = 1000,
) -> BehaviorTree:
    """Wait until no alive enemy with ``model_id`` is left in the area, then
    repeat the wait ``waves`` times. Each wave returns SUCCESS as soon as
    no matching enemies are present, or when ``timeout_ms`` is reached
    (skip-and-continue semantics, mirroring the legacy Wait_for_Spawns).

    Between waves, the leader is nudged back to ``hold_position`` (if set)
    after a short ``delay_between_waves_ms`` pause, so newly spawned
    Mindblades that arrive after a brief lull are still picked up by the
    next wave. Adapted from underworld.py::Wait_for_Spawns (model 2380).

    If ``hold_position`` is provided, the leader is also repeatedly nudged
    back to that position whenever they drift further than
    ``hold_tolerance`` units away while a wave is RUNNING.
    """

    def _maybe_hold():
        if hold_position is None:
            return
        try:
            px, py = Player.GetXY()
        except Exception:
            return
        dx = px - hold_position.x
        dy = py - hold_position.y
        if (dx * dx + dy * dy) > (hold_tolerance * hold_tolerance):
            Player.Move(hold_position.x, hold_position.y)

    def _condition() -> BehaviorTree.NodeState:
        enemies = [
            agent_id for agent_id in AgentArray.GetEnemyArray()
            if Agent.IsAlive(agent_id) and Agent.GetModelID(agent_id) == model_id
        ]
        if not enemies:
            return BehaviorTree.NodeState.SUCCESS
        # Keep the leader anchored at the configured hold position so the
        # team does not drift while waiting for the spawn to despawn.
        _maybe_hold()
        return BehaviorTree.NodeState.RUNNING

    def _on_timeout_success(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        # WaitUntilNode returns FAILURE on timeout; wrap in a Selector so we
        # treat timeout as "give up and continue" instead of failing the run.
        return BehaviorTree.NodeState.SUCCESS

    def _move_back_to_hold(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        if hold_position is not None:
            Player.Move(hold_position.x, hold_position.y)
        return BehaviorTree.NodeState.SUCCESS

    def _build_single_wave(wave_index: int) -> "BehaviorTree.Node":
        wave_name = f"{name}::Wave{wave_index}"
        return BehaviorTree.SelectorNode(
            name=wave_name,
            children=[
                BehaviorTree.WaitUntilNode(
                    condition_fn=_condition,
                    throttle_interval_ms=throttle_interval_ms,
                    timeout_ms=timeout_ms,
                    name=f"{wave_name}::Wait",
                ),
                BehaviorTree.ActionNode(
                    name=f"{wave_name}::TimeoutContinue",
                    action_fn=_on_timeout_success,
                ),
            ],
        )

    wave_count = max(1, int(waves))
    children: list["BehaviorTree.Node"] = []
    for wave_index in range(1, wave_count + 1):
        children.append(_build_single_wave(wave_index))
        if wave_index < wave_count:
            # Brief pause + nudge back to the hold position before the next
            # wave, so freshly spawned Mindblades are still detected.
            children.append(
                BehaviorTree.WaitNode(
                    check_fn=lambda: BehaviorTree.NodeState.RUNNING,
                    timeout_ms=max(1, int(delay_between_waves_ms)),
                    name=f"{name}::WaveDelay{wave_index}",
                )
            )
            children.append(
                BehaviorTree.ActionNode(
                    name=f"{name}::WaveMoveBack{wave_index}",
                    action_fn=_move_back_to_hold,
                )
            )

    return BehaviorTree(
        root=BehaviorTree.SequenceNode(
            name=name,
            children=children,
        )
    )


def _clear_party_flags(*, name: str = "ClearPartyFlags") -> BehaviorTree:
    """Clear all native GW hero flags + HeroAI multibox flags (mirrors the
    legacy ``UWHeroAIAdapter.clear_flags``). Always returns SUCCESS."""

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        try:
            GLOBAL_CACHE.Party.Heroes.UnflagAllHeroes()
        except Exception:
            pass
        try:
            for _, options in GLOBAL_CACHE.ShMem.GetAllActiveAccountHeroAIPairs(sort_results=False):
                options.IsFlagged = False
                options.FlagPos.x = 0.0
                options.FlagPos.y = 0.0
                options.AllFlag.x = 0.0
                options.AllFlag.y = 0.0
        except Exception:
            pass
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        root=BehaviorTree.ActionNode(name=name, action_fn=_tick)
    )


def _spread_party_flags(
    flag_points: list[Vec2f],
    *,
    name: str = "SpreadPartyFlags",
    clear_first: bool = True,
) -> BehaviorTree:
    """Assign one flag per party position to the matching coordinate in
    ``flag_points`` (party position = index + 1). Sets both the native GW
    hero flag (local heroes) and the HeroAI shared-memory flag (multibox
    followers), mirroring ``UWHeroAIAdapter.set_flag``.

    If ``clear_first`` is True, all flags are cleared before the new ones
    are set so stale flags from previous quest legs are not kept.
    """

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        if clear_first:
            try:
                GLOBAL_CACHE.Party.Heroes.UnflagAllHeroes()
            except Exception:
                pass
            try:
                for _, options in GLOBAL_CACHE.ShMem.GetAllActiveAccountHeroAIPairs(sort_results=False):
                    options.IsFlagged = False
                    options.FlagPos.x = 0.0
                    options.FlagPos.y = 0.0
                    options.AllFlag.x = 0.0
                    options.AllFlag.y = 0.0
            except Exception:
                pass

        for index, point in enumerate(flag_points):
            party_pos = index + 1

            # Native GW hero flag (local heroes).
            try:
                agent_id = GLOBAL_CACHE.Party.Heroes.GetHeroAgentIDByPartyPosition(party_pos)
                if agent_id and Agent.IsValid(agent_id):
                    GLOBAL_CACHE.Party.Heroes.FlagHero(agent_id, point.x, point.y)
            except Exception:
                pass

            # HeroAI shared-memory flag (multibox-account followers).
            try:
                options = GLOBAL_CACHE.ShMem.GetHeroAIOptionsByPartyNumber(party_pos)
                if options is not None:
                    options.IsFlagged = True
                    options.FlagPos.x = float(point.x)
                    options.FlagPos.y = float(point.y)
                    options.FlagFacingAngle = 0.0
            except Exception:
                pass

        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        root=BehaviorTree.ActionNode(name=name, action_fn=_tick)
    )


def _wait_until_no_mindblades(
    *,
    name: str = "WaitUntilNoMindblades",
    timeout_ms: int = 20_000,
    hold_position: Vec2f | None = None,
    waves: int = 4,
    delay_between_waves_ms: int = 1000,
) -> BehaviorTree:
    """Wait until no alive Mindblade Spectre is left, repeated ``waves``
    times so freshly spawned Mindblades after a brief lull are still
    picked up. Each wave times out after ``timeout_ms``."""
    return _wait_until_no_alive_model(
        MINDBLADE_MODEL_ID,
        name=name,
        timeout_ms=timeout_ms,
        hold_position=hold_position,
        waves=waves,
        delay_between_waves_ms=delay_between_waves_ms,
    )


# ╔══════════════════════════════════════════════════════════════════
# ║                       PLANNER STEP BUILDERS
# ╠══════════════════════════════════════════════════════════════════
# ║  Each builder returns a BehaviorTree. Together they form the run
# ║  sequence executed top-to-bottom by BottingTree. Replace the
# ║  _todo_step(...) placeholders with real subtrees as the bot is
# ║  rebuilt section by section.
# ╚══════════════════════════════════════════════════════════════════

def _build_prepare_outpost_tree() -> BehaviorTree:
    """ToA preparation: enable required widgets on all accounts, travel everyone
    to the Guild Hall, then run MerchantRules (Preview + Execute) on every
    account. Conset / pcon refill and party setup will plug in here later."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="PrepareOutpost",
            children=[
                BTF.ApplyWidgetPolicyOnAllAccounts(enable_widgets=REQUIRED_WIDGETS).root,
                BTF.LeavePartyOnAllAccounts().root,
                BTF.TravelAllAccountsToGuildHall().root,
                BTF.RunMerchantRulesOnAllAccounts().root,
            ],
        )
    )


def _build_enter_underworld_tree() -> BehaviorTree:
    """Travel the leader to the configured UW entry outpost on a random
    low-population district, pull every follower to the same district, invite
    everyone into the leader's party, then talk to the Reaper of the
    Labyrinth and zone into UW."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="EnterUnderworld",
            children=[
                BTF.TravelLeaderToOutpostThenFollowers(
                    target_map_id=lambda: GetSelectedEntrypoint()[1],
                ).root,
                BTF.InviteAllAccountsToParty().root,
                BTF.UsePassageScrollOnRandomAccount(
                    scroll_model_id=UW_SCROLL_MODEL_ID,
                    target_map_id=UW_MAP_ID,
                    scroll_label="Passage Scroll to the Underworld",
                ).root,
            ],
        )
    )


def _build_chamber_tree() -> BehaviorTree:
    """Chamber: talk to the Lost Soul to pick up the first quest
    ("Clear the Chamber"), then walk the chamber clearing path to pull
    and clear the entry chamber spawns."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="Chamber",
            children=[
                BT.MoveAndAutoDialogByModelID(LOST_SOUL_MODEL_ID).root,
                BT.Move(CHAMBER_PATH, pause_on_combat=True).root,
                # Accept "Clear the Chamber" Quest Reward
                BT.MoveAndAutoDialogByModelID(REAPER_OF_THE_LABYRINTH_MODEL_ID).root,
                # Take "Restoring Grenth's Monuments" quest from the same Reaper.
                BT.TargetAndDialogByModelID(REAPER_OF_THE_LABYRINTH_MODEL_ID, 0x806D01).root,
            ],
        )
    )


def _build_restore_mountains_tree() -> BehaviorTree:
    """Restore the Mountains: walk the perimeter path to clear the
    Mountains spawns for the "Restoring Grenth's Monuments" quest."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="RestoreMountains",
            children=[
                BT.Move(RESTORE_MOUNTAINS_PATH, pause_on_combat=True).root,
            ],
        )
    )


def _build_demon_assassin_tree() -> BehaviorTree:
    """Demon Assassin: take the quest from the Reaper of the Twin Serpent
    Mountains, walk to the pull spot, and wait until the quest is
    completed."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="DemonAssassin",
            children=[
                BT.TargetAndDialogByModelID(REAPER_OF_THE_MOUNTAINS_MODEL_ID, 0x806801).root,
                BT.Move(DEMON_ASSASSIN_PATH, pause_on_combat=True).root,
                _wait_until_active_quest_completed(
                    name="WaitDemonAssassinComplete",
                ).root,
            ],
        )
    )


def _build_restore_chaos_planes_tree() -> BehaviorTree:
    """Restore the Chaos Planes: walk the path to clear the Chaos Planes
    spawns for the "Restoring Grenth's Monuments" quest. Banished Dream
    Riders are blacklisted for the duration of the path so the team does
    not engage them along the way."""
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="RestoreChaosPlanes",
            children=[
                _blacklist_enemy_name(
                    "banished dream rider",
                    name="Blacklist Banished Dream Rider",
                ).root,
                BT.Move(RESTORE_CHAOS_PLANES_PATH, pause_on_combat=True).root,
                _unblacklist_enemy_name(
                    "banished dream rider",
                    name="Unblacklist Banished Dream Rider",
                ).root,
                _wait_until_no_mindblades(
                    name="WaitNoMindblades",
                    timeout_ms=20_000,
                    hold_position=CHAOS_PLANES_MINDBLADE_HOLD_POSITION,
                ).root,
                BT.Move(
                    [CHAOS_PLANES_MINDBLADE_HOLD_POSITION_2],
                    pause_on_combat=True,
                ).root,
                _wait_until_no_mindblades(
                    name="WaitNoMindblades2",
                    timeout_ms=20_000,
                    hold_position=CHAOS_PLANES_MINDBLADE_HOLD_POSITION_2,
                ).root,
            ],
        )
    )


def _build_wailing_lord_tree() -> BehaviorTree:
    """Wailing Lord."""
    return _todo_step("WailingLord")


def _build_terrorweb_queen_tree() -> BehaviorTree:
    """Terrorweb Queen."""
    return _todo_step("TerrorwebQueen")


def _build_vengeful_aatxes_tree() -> BehaviorTree:
    """Vengeful Aatxes / Vanguard."""
    return _todo_step("VengefulAatxes")


def _build_servants_of_grenth_tree() -> BehaviorTree:
    """Servants of Grenth."""
    return _todo_step("ServantsOfGrenth")


def _build_pits_tree() -> BehaviorTree:
    """Pools of Anguish (Pits)."""
    return _todo_step("Pits")


def _build_mountains_tree() -> BehaviorTree:
    """Mountains (Pools / Coldfire Nights)."""
    return _todo_step("Mountains")


def _build_plains_tree() -> BehaviorTree:
    """Plains (Plagueborn / Dreamriders)."""
    return _todo_step("Plains")


def _build_four_horsemen_tree() -> BehaviorTree:
    """The Four Horsemen quest leg.

    Phases (mirrors legacy The_Four_Horsemen):
      1. Move to the pre-position in the Chaos Planes / Mountains border,
         brief warm-up wait so HeroAI catches up.
      2. Walk to the Reaper of the Chaos Planes and take the quest.
      3. Trigger the Reaper's "Tp Lab" dialog (0x8D) which sends the
         party to the Labyrinth, then walk back to the Reaper of the
         Labyrinth and use the "Tp back to Chaos" dialog (0x8B) to
         return to the Chaos Planes (this is what actually spawns the
         Horsemen).
      4. Move to the hold spot and wait until the quest is completed.
      5. Walk back to the Reaper of the Chaos Planes and take the
         quest reward (0x806A07).
    """
    return BehaviorTree(
        BehaviorTree.SequenceNode(
            name="FourHorsemen",
            children=[
                # Phase 1: pre-position before talking to the Reaper.
                BT.Move([FOUR_HORSEMEN_PRE_POSITION], pause_on_combat=True).root,
                # Spread hero flags around the pre-position so the team fans
                # out while HeroAI / followers settle (mirrors legacy 10s wait).
                _spread_party_flags(
                    FOUR_HORSEMEN_PRE_FLAG_POINTS,
                    name="SpreadFourHorsemenPreFlags",
                ).root,
                #BT.Wait(10_000).root,
                # Phase 2: take the Four Horsemen quest from the Chaos Planes Reaper.
                BT.Move([FOUR_HORSEMEN_NPC_POSITION], pause_on_combat=True).root,
                BT.TargetAndDialogByModelID(
                    REAPER_OF_THE_CHAOS_PLANES_MODEL_ID, 0x806A01
                ).root,
                # 32s warm-up so HeroAI / followers settle before the TP loop.
                BT.Wait(32_000).root,
                # Phase 3: TP loop via Labyrinth to spawn the Horsemen.
                _clear_party_flags(name="ClearFourHorsemenFlags").root,
                BT.TargetAndDialogByModelID(
                    REAPER_OF_THE_CHAOS_PLANES_MODEL_ID, 0x8D
                ).root,
                #BT.Move([FOUR_HORSEMEN_LAB_RETURN_POSITION], pause_on_combat=True).root,
                BT.Wait(2_000).root,
                BT.TargetAndDialogByModelID(
                    REAPER_OF_THE_LABYRINTH_MODEL_ID, 0x8B
                ).root,
                # Phase 4: hold position and wait for the quest to complete.
                # Spread all hero/follower flags onto the hold spot so the
                # team stacks on the leader instead of wandering off.
                _spread_party_flags(
                    FOUR_HORSEMEN_HOLD_FLAG_POINTS,
                    name="SpreadFourHorsemenHoldFlags",
                ).root,
                BT.Move([FOUR_HORSEMEN_HOLD_FLAG_POINTS[0]], pause_on_combat=True).root,
                _wait_until_active_quest_completed(
                    name="WaitFourHorsemenComplete",
                ).root,
                # Clear flags so the team can move freely again before the
                # reward dialog.
                _clear_party_flags(name="ClearFourHorsemenFlags").root,
                # Phase 5: take the quest reward from the Reaper of the Chaos Planes.
                BT.Move([FOUR_HORSEMEN_NPC_POSITION], pause_on_combat=True).root,
                BT.TargetAndDialogByModelID(
                    REAPER_OF_THE_CHAOS_PLANES_MODEL_ID, 0x806A07
                ).root,
            ],
        )
    )


def _build_dhuum_tree() -> BehaviorTree:
    """Dhuum: final encounter."""
    return _todo_step("Dhuum")


def _build_finalize_run_tree() -> BehaviorTree:
    """Quest reward, leave UW, return to ToA."""
    return _todo_step("FinalizeRun")


def _get_sequence_builders():
    """Ordered list of (step_name, builder) tuples that make up one full UW run."""
    return [
        ("PrepareOutpost",     _build_prepare_outpost_tree),
        ("EnterUnderworld",    _build_enter_underworld_tree),
        ("Chamber",            _build_chamber_tree),
        ("RestoreMountains",   _build_restore_mountains_tree),
        ("DemonAssassin",      _build_demon_assassin_tree),
        ("RestoreChaosPlanes", _build_restore_chaos_planes_tree),
        ("FourHorsemen",       _build_four_horsemen_tree),
        ("WailingLord",        _build_wailing_lord_tree),
        ("TerrorwebQueen",     _build_terrorweb_queen_tree),
        ("VengefulAatxes",     _build_vengeful_aatxes_tree),
        ("ServantsOfGrenth",   _build_servants_of_grenth_tree),
        ("Pits",               _build_pits_tree),
        ("Mountains",          _build_mountains_tree),
        ("Plains",             _build_plains_tree),
        ("Dhuum",              _build_dhuum_tree),
        ("FinalizeRun",        _build_finalize_run_tree),
    ]


# ╔══════════════════════════════════════════════════════════════════
# ║                       INI CONFIGURATION
# ╚══════════════════════════════════════════════════════════════════

def _add_config_vars():
    """Register all persisted configuration variables for this bot."""
    ini = IniManager()
    # Display
    ini.add_bool(INI_KEY, "show_tree",              "Display",  "ShowTree",            default=True)
    # Behavior
    ini.add_bool(INI_KEY, "enable_headless_heroai", "Behavior", "EnableHeadlessHeroAI", default=True)
    ini.add_bool(INI_KEY, "auto_restart",           "Behavior", "AutoRestart",         default=True)
    ini.add_bool(INI_KEY, "debug_logging",          "Behavior", "DebugLogging",        default=False)
    # Run options
    ini.add_str(INI_KEY,  "entrypoint",             "Run",      "EntryPoint",          default=DEFAULT_UW_ENTRYPOINT_KEY)


# ╔══════════════════════════════════════════════════════════════════
# ║                       SETTINGS HELPERS
# ╚══════════════════════════════════════════════════════════════════

def GetSelectedEntrypointKey() -> str:
    """Return the currently configured UW entry-outpost ini key."""
    if not INI_KEY:
        return DEFAULT_UW_ENTRYPOINT_KEY
    key = IniManager().getStr(INI_KEY, "entrypoint", DEFAULT_UW_ENTRYPOINT_KEY)
    return key if key in UW_ENTRYPOINTS else DEFAULT_UW_ENTRYPOINT_KEY


def GetSelectedEntrypoint() -> tuple[str, int]:
    """Return (display_name, map_id) for the currently configured entrypoint."""
    return UW_ENTRYPOINTS[GetSelectedEntrypointKey()]


def _draw_settings_tab() -> None:
    """Custom Settings tab: replicates the BottingTree default options and
    adds an UW-specific entry-outpost combobox."""
    if botting_tree is None:
        return

    # ── Default BottingTree settings ─────────────────────────────────────────
    botting_tree.pause_on_combat = PyImGui.checkbox(
        "Pause Planner On Combat", botting_tree.pause_on_combat
    )

    headless = PyImGui.checkbox("Headless HeroAI", botting_tree.IsHeadlessHeroAIEnabled())
    if headless != botting_tree.IsHeadlessHeroAIEnabled():
        botting_tree.SetHeadlessHeroAIEnabled(headless, reset_runtime=False)

    looting = PyImGui.checkbox("Looting", botting_tree.IsLootingEnabled())
    if looting != botting_tree.IsLootingEnabled():
        botting_tree.SetLootingEnabled(looting)

    isolation = PyImGui.checkbox("Account Isolation", botting_tree.IsIsolationEnabled())
    if isolation != botting_tree.IsIsolationEnabled():
        botting_tree.SetIsolationEnabled(isolation)

    PyImGui.separator()
    botting_tree.DrawMovePathDebugOptions()
    # ── Debug ───────────────────────────────────────────────────────────────────────
    PyImGui.separator()
    ini = IniManager()
    debug_enabled = bool(ini.getBool(INI_KEY, "debug_logging", False))
    new_debug = PyImGui.checkbox("Verbose Debug Logging", debug_enabled)
    if new_debug != debug_enabled:
        ini.set(INI_KEY, "debug_logging", new_debug)
        ini.save_vars(INI_KEY)
        BTF.SetDebugLogging(new_debug)
    # ── UW-specific options ──────────────────────────────────────────────────
    PyImGui.separator()
    PyImGui.text("Underworld Run")

    entrypoint_keys   = list(UW_ENTRYPOINTS.keys())
    entrypoint_labels = [label for label, _ in UW_ENTRYPOINTS.values()]
    current_key = GetSelectedEntrypointKey()
    current_idx = entrypoint_keys.index(current_key) if current_key in entrypoint_keys else 0

    PyImGui.text("Entry Outpost:")
    new_idx = PyImGui.combo("##uwv2_entrypoint", current_idx, entrypoint_labels)
    if new_idx != current_idx and 0 <= new_idx < len(entrypoint_keys):
        new_key = entrypoint_keys[new_idx]
        ini = IniManager()
        ini.set(INI_KEY, "entrypoint", new_key)
        ini.save_vars(INI_KEY)


# ╔══════════════════════════════════════════════════════════════════
# ║                       MAIN ENTRYPOINT
# ╚══════════════════════════════════════════════════════════════════

def main():
    global INI_KEY, initialized, botting_tree

    if not initialized:
        if not INI_KEY:
            INI_KEY = IniManager().ensure_key(INI_PATH, INI_FILENAME)
            if not INI_KEY:
                return
            _add_config_vars()
            IniManager().load_once(INI_KEY)

        botting_tree = BottingTree(BOT_NAME, isolation_enabled=False)
        botting_tree.SetNamedPlannerSteps(
            _get_sequence_builders(),
            start_from="PrepareOutpost",
            name="UnderworldRun",
        )
        botting_tree.UI.override_draw_config(_draw_settings_tab)
        # Apply persisted debug-logging flag to BTF helpers.
        BTF.SetDebugLogging(bool(IniManager().getBool(INI_KEY, "debug_logging", False)))
        initialized = True

    if botting_tree is not None:
        botting_tree.tick()
        botting_tree.UI.draw_window(
            main_child_dimensions=(450, 450),
            icon_path=MODULE_ICON,
            iconwidth=128,
        )


if __name__ == "__main__":
    main()
