# ╔══════════════════════════════════════════════════════════════════════════════
# ║  File    : BottingTreeFunctions.py
# ║  Purpose : Custom BehaviorTree helper functions for BottingTree-based bots
# ║            in this folder (currently UnderworldV2). Add helpers here ONLY
# ║            when the functionality does not already exist in
# ║            Py4GWCoreLib.BottingTree, RoutinesBT (BehaviourTrees), or the
# ║            ApoSource wrappers.
# ╚══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import os
import random
from typing import Callable, Union

import Py4GW
from Py4GWCoreLib import GLOBAL_CACHE, Map, Party, Player
from Py4GWCoreLib.enums_src.Multiboxing_enums import SharedCommandType
from Py4GWCoreLib.enums_src.Region_enums import District
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Py4GWCoreLib.py4gwcorelib_src.IniHandler import IniHandler
from Py4GWCoreLib.py4gwcorelib_src.WidgetManager import get_widget_handler


_LOG_SOURCE = "BottingTreeFunctions"

# Toggle from the bot UI (e.g. UnderworldV2 settings tab) via SetDebugLogging.
_DEBUG_ENABLED: bool = False


def SetDebugLogging(enabled: bool) -> None:
    """Enable/disable the verbose `_dlog` traces emitted by every helper here."""
    global _DEBUG_ENABLED
    new_value = bool(enabled)
    if new_value == _DEBUG_ENABLED:
        return
    _DEBUG_ENABLED = new_value
    Py4GW.Console.Log(
        _LOG_SOURCE,
        f"Debug logging {'enabled' if new_value else 'disabled'}.",
        Py4GW.Console.MessageType.Info,
    )


def IsDebugLoggingEnabled() -> bool:
    return _DEBUG_ENABLED


def _log(message: str, level=Py4GW.Console.MessageType.Info) -> None:
    """Light wrapper around Py4GW.Console.Log so callers stay short."""
    Py4GW.Console.Log(_LOG_SOURCE, message, level)


def _dlog(message: str) -> None:
    """Verbose log gated by SetDebugLogging(True)."""
    if _DEBUG_ENABLED:
        Py4GW.Console.Log(_LOG_SOURCE, f"[debug] {message}", Py4GW.Console.MessageType.Debug)


# ╔══════════════════════════════════════════════════════════════════
# ║                       WIDGET POLICY HELPERS
# ╚══════════════════════════════════════════════════════════════════

def ApplyWidgetPolicyOnAllAccounts(
    enable_widgets: tuple[str, ...] = (),
    disable_widgets: tuple[str, ...] = (),
    fanout_wait_ms: int = 1500,
) -> BehaviorTree:
    """Enable / disable a set of widgets on the leader and on every other
    running account.

    On the leader the widget state is changed in-process via the
    ``WidgetHandler``. For every other account a multibox message is sent
    (``EnableWidget`` / ``DisableWidget``) and the receiver-side messaging
    handler applies the change. After the fanout the tree waits
    ``fanout_wait_ms`` to give followers time to react.

    Returns SUCCESS once dispatch has finished (it does not verify the
    follower-side state).
    """

    def _apply_local() -> BehaviorTree.NodeState:
        _dlog(
            f"ApplyWidgetPolicy: enable={list(enable_widgets)}, "
            f"disable={list(disable_widgets)}"
        )
        widget_handler = get_widget_handler()

        for widget_name in enable_widgets:
            currently_on = widget_handler.is_widget_enabled(widget_name)
            _dlog(f"  local '{widget_name}' currently_on={currently_on}")
            if not currently_on:
                widget_handler.enable_widget(widget_name)
                _log(f"Leader: enabled widget '{widget_name}'.")
        for widget_name in disable_widgets:
            currently_on = widget_handler.is_widget_enabled(widget_name)
            _dlog(f"  local '{widget_name}' currently_on={currently_on}")
            if currently_on:
                widget_handler.disable_widget(widget_name)
                _log(f"Leader: disabled widget '{widget_name}'.")
        return BehaviorTree.NodeState.SUCCESS

    def _fanout() -> BehaviorTree.NodeState:
        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]
        _dlog(
            f"ApplyWidgetPolicyFanout: sender={sender_email}, "
            f"total_accounts={len(all_accounts)}, followers={len(followers)}"
        )
        if not followers:
            return BehaviorTree.NodeState.SUCCESS

        for account in followers:
            target = str(account.AccountEmail)
            for widget_name in enable_widgets:
                _dlog(f"  -> EnableWidget '{widget_name}' to {target}")
                GLOBAL_CACHE.ShMem.SendMessage(
                    sender_email,
                    target,
                    SharedCommandType.EnableWidget,
                    (0, 0, 0, 0),
                    (widget_name, "", "", ""),
                )
            for widget_name in disable_widgets:
                _dlog(f"  -> DisableWidget '{widget_name}' to {target}")
                GLOBAL_CACHE.ShMem.SendMessage(
                    sender_email,
                    target,
                    SharedCommandType.DisableWidget,
                    (0, 0, 0, 0),
                    (widget_name, "", "", ""),
                )

        if enable_widgets:
            _log(f"Sent EnableWidget {list(enable_widgets)} to {len(followers)} follower(s).")
        if disable_widgets:
            _log(f"Sent DisableWidget {list(disable_widgets)} to {len(followers)} follower(s).")
        return BehaviorTree.NodeState.SUCCESS

    root = BehaviorTree.SequenceNode(
        name="ApplyWidgetPolicyOnAllAccounts",
        children=[
            BehaviorTree.ActionNode(
                name="ApplyWidgetPolicyLocal",
                action_fn=_apply_local,
                aftercast_ms=0,
            ),
            BehaviorTree.ActionNode(
                name="ApplyWidgetPolicyFanout",
                action_fn=_fanout,
                aftercast_ms=fanout_wait_ms,
            ),
        ],
    )

    return BehaviorTree(root)


# ╔══════════════════════════════════════════════════════════════════
# ║                    MULTIBOX / GUILD HALL HELPERS
# ╚══════════════════════════════════════════════════════════════════

def LeavePartyOnAllAccounts(
    settle_ms: int = 1500,
) -> BehaviorTree:
    """Make every running account leave its current party.

    Reuses HeroAI's ``Leave & Travel to GH`` command for the followers (it
    sends ``SharedCommandType.LeaveParty`` + ``TravelToGuildHall``); the
    leader leaves locally via ``GLOBAL_CACHE.Party.LeaveParty()``.

    Note: this also kicks the followers towards their own Guild Hall, which
    is the same behavior as the HeroAI button. Pair this with
    :func:`TravelAllAccountsToGuildHall` to pull everyone into the leader's
    Guild Hall afterwards.
    """

    def _action() -> BehaviorTree.NodeState:
        try:
            from HeroAI.commands import HeroAICommands
        except Exception as exc:
            _log(
                f"LeavePartyOnAllAccounts: failed to load HeroAI.commands ({exc}).",
                Py4GW.Console.MessageType.Warning,
            )
            return BehaviorTree.NodeState.FAILURE

        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]

        # Leader: leave own party in-process (HeroAI command does not target
        # the local account).
        try:
            GLOBAL_CACHE.Party.LeaveParty()
            _log("Leader: left party.")
        except Exception as exc:
            _log(
                f"Leader: LeaveParty() failed ({exc}).",
                Py4GW.Console.MessageType.Warning,
            )

        if followers:
            try:
                HeroAICommands().LeavePartyAndTravelGH(followers)
                _log(
                    f"Leader: dispatched HeroAI 'Leave & Travel to GH' "
                    f"to {len(followers)} account(s)."
                )
            except Exception as exc:
                _log(
                    f"LeavePartyOnAllAccounts: HeroAICommands.LeavePartyAndTravelGH "
                    f"failed: {exc}",
                    Py4GW.Console.MessageType.Error,
                )
                return BehaviorTree.NodeState.FAILURE
        else:
            _dlog("No follower accounts detected; only leader left party.")
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.ActionNode(
            name="LeavePartyOnAllAccounts",
            action_fn=_action,
            aftercast_ms=settle_ms,
        )
    )


def TravelAllAccountsToGuildHall(
    travel_timeout_ms: int = 60_000,
    sync_timeout_ms: int = 60_000,
    poll_ms: int = 500,
) -> BehaviorTree:
    """Travel every running account to the leader's Guild Hall.

    Behavior:
      1. If the leader is not already in a Guild Hall, dispatches `Map.TravelGH()`
         and waits for the leader's map to be ready (up to `travel_timeout_ms`).
      2. Sends `SharedCommandType.TravelToGuildHall` to every other account
         registered in shared memory.
      3. Waits until every account reports the same `MapID` as the leader, or
         until `sync_timeout_ms` elapses (in which case the node still succeeds
         and the bot continues).

    Returns SUCCESS once the synchronization step finishes (or times out).
    """

    state: dict = {
        "followers_notified": False,
        "leader_map_id": 0,
    }

    # ── Phase 1: leader to GH ────────────────────────────────────────────────
    def _leader_travel_action() -> BehaviorTree.NodeState:
        _dlog(
            f"TravelAllAccountsToGuildHall: leader map={Map.GetMapID()}, "
            f"is_gh={Map.IsGuildHall()}, is_ready={Map.IsMapReady()}"
        )
        if Map.IsGuildHall():
            state["leader_map_id"] = int(Map.GetMapID())
            _log(f"Leader already in Guild Hall (map {state['leader_map_id']}).")
            return BehaviorTree.NodeState.SUCCESS
        _log("Traveling leader to Guild Hall.")
        Map.TravelGH()
        return BehaviorTree.NodeState.SUCCESS

    def _leader_arrived() -> BehaviorTree.NodeState:
        if Map.IsMapReady() and Map.IsGuildHall():
            state["leader_map_id"] = int(Map.GetMapID())
            _log(f"Leader arrived in Guild Hall (map {state['leader_map_id']}).")
            return BehaviorTree.NodeState.SUCCESS
        if not state.get("leader_gh_wait_logged"):
            state["leader_gh_wait_logged"] = True
            _dlog(
                f"Waiting for leader to arrive in Guild Hall "
                f"(map={Map.GetMapID()}, is_gh={Map.IsGuildHall()}, "
                f"is_ready={Map.IsMapReady()})."
            )
        return BehaviorTree.NodeState.RUNNING

    leader_branch = BehaviorTree.SequenceNode(
        name="LeaderTravelGH",
        children=[
            BehaviorTree.ActionNode(
                name="LeaderTravelGHAction",
                action_fn=_leader_travel_action,
                aftercast_ms=3000,
            ),
            BehaviorTree.WaitNode(
                name="LeaderTravelGHWait",
                check_fn=_leader_arrived,
                timeout_ms=travel_timeout_ms,
            ),
        ],
    )

    # ── Phase 2: notify followers ────────────────────────────────────────────
    def _notify_followers() -> BehaviorTree.NodeState:
        if state["followers_notified"]:
            return BehaviorTree.NodeState.SUCCESS

        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]
        if not followers:
            _log("No follower accounts detected; skipping fanout.")
            state["followers_notified"] = True
            return BehaviorTree.NodeState.SUCCESS

        _log(f"Sending TravelToGuildHall to {len(followers)} follower(s).")
        for account in followers:
            _dlog(f"  -> TravelToGuildHall to {account.AccountEmail}")
            GLOBAL_CACHE.ShMem.SendMessage(
                sender_email,
                str(account.AccountEmail),
                SharedCommandType.TravelToGuildHall,
                (0, 0, 0, 0),
            )
        state["followers_notified"] = True
        return BehaviorTree.NodeState.SUCCESS

    notify_branch = BehaviorTree.ActionNode(
        name="NotifyFollowersTravelGH",
        action_fn=_notify_followers,
        aftercast_ms=1500,
    )

    # ── Phase 3: wait for everyone on leader's map ───────────────────────────
    def _all_accounts_in_gh() -> BehaviorTree.NodeState:
        leader_map_id = int(state.get("leader_map_id") or Map.GetMapID() or 0)
        if leader_map_id <= 0:
            return BehaviorTree.NodeState.RUNNING

        accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        if not accounts:
            return BehaviorTree.NodeState.SUCCESS

        mismatched = tuple(
            sorted(
                str(acc.AccountEmail)
                for acc in accounts
                if int(acc.AgentData.Map.MapID) != leader_map_id
            )
        )

        if not mismatched:
            _log("All accounts arrived in Guild Hall.")
            return BehaviorTree.NodeState.SUCCESS

        # Log only when the set of pending accounts changes (no per-tick spam).
        if state.get("gh_sync_pending") != mismatched:
            state["gh_sync_pending"] = mismatched
            _dlog(f"GH sync waiting on: {list(mismatched)}")
        return BehaviorTree.NodeState.RUNNING

    sync_branch = BehaviorTree.WaitNode(
        name="WaitAllAccountsInGuildHall",
        check_fn=_all_accounts_in_gh,
        timeout_ms=sync_timeout_ms,
    )

    root = BehaviorTree.SequenceNode(
        name="TravelAllAccountsToGuildHall",
        children=[
            leader_branch,
            notify_branch,
            sync_branch,
        ],
    )

    return BehaviorTree(root)

# Districts considered "low population" — used for stealthy outpost travel.
_UNPOPULAR_DISTRICTS: tuple[int, ...] = (
    District.EuropeItalian.value,
    District.EuropeSpanish.value,
    District.EuropePolish.value,
    District.EuropeRussian.value,
    District.AsiaKorean.value,
    District.AsiaJapanese.value,
)


def TravelLeaderToOutpostThenFollowers(
    target_map_id: Union[int, Callable[[], int]],
    districts: tuple[int, ...] = _UNPOPULAR_DISTRICTS,
    leader_travel_timeout_ms: int = 60_000,
    follower_sync_timeout_ms: int = 90_000,
    follower_dispatch_settle_ms: int = 1500,
) -> BehaviorTree:
    """Travel the leader into ``target_map_id`` on a random low-population
    district, then pull every follower into the *same* map + district.

    Behavior:
      1. Leader picks a random district from ``districts`` (default: a curated
         set of typically empty European / Asian districts) and travels there
         via ``Map.TravelToDistrict(target_map_id, district=...)``. Waits up to
         ``leader_travel_timeout_ms`` for the map to be ready and matching.
      2. Sends ``SharedCommandType.TravelToMap`` to every follower with the
         leader's actual ``(MapID, Region, District, Language)`` so every
         follower lands in the very same district instance.
      3. Waits until every account reports the same MapID *and* District as
         the leader, or until ``follower_sync_timeout_ms`` elapses (in which
         case the node still succeeds and the bot continues).

    The ``target_map_id`` argument may be an integer or a zero-arg callable
    (handy when the value comes from a settings UI that may change between
    runs).
    """

    state: dict = {
        "target_map_id": 0,
        "chosen_district": 0,
        "leader_arrived": False,
    }

    def _resolve_target_map_id() -> int:
        if callable(target_map_id):
            try:
                return int(target_map_id())
            except Exception:
                return 0
        try:
            return int(target_map_id)
        except Exception:
            return 0

    # ── Phase 1: leader travels to a random unpopular district ───────────────
    def _leader_travel_action() -> BehaviorTree.NodeState:
        map_id = _resolve_target_map_id()
        _dlog(
            f"TravelLeaderToOutpost: requested map={map_id}, "
            f"current map={Map.GetMapID()}, district={Map.GetDistrict()}, "
            f"is_ready={Map.IsMapReady()}"
        )
        if map_id <= 0:
            _log(
                "Leader travel skipped: target map id is invalid.",
                Py4GW.Console.MessageType.Warning,
            )
            return BehaviorTree.NodeState.FAILURE

        state["target_map_id"] = map_id

        if int(Map.GetMapID()) == map_id and Map.IsMapReady():
            current_district = int(Map.GetDistrict())
            state["chosen_district"] = current_district
            state["leader_arrived"] = True
            _log(f"Leader already in target outpost (map {map_id}, district {current_district}).")
            return BehaviorTree.NodeState.SUCCESS

        chosen = int(random.choice(list(districts))) if districts else 0
        state["chosen_district"] = chosen
        _log(f"Leader: travel to map {map_id} on district {chosen}.")
        _dlog(f"  candidate districts={list(districts)} -> chosen={chosen}")
        Map.TravelToDistrict(map_id, district=chosen)
        return BehaviorTree.NodeState.SUCCESS

    def _leader_arrived() -> BehaviorTree.NodeState:
        if state["leader_arrived"]:
            return BehaviorTree.NodeState.SUCCESS
        if Map.IsMapReady() and int(Map.GetMapID()) == int(state["target_map_id"]):
            state["leader_arrived"] = True
            _log(
                f"Leader arrived in map {state['target_map_id']} "
                f"(district {int(Map.GetDistrict())})."
            )
            return BehaviorTree.NodeState.SUCCESS
        if not state.get("leader_outpost_wait_logged"):
            state["leader_outpost_wait_logged"] = True
            _dlog(
                f"Waiting for leader to arrive in outpost "
                f"(target={state['target_map_id']}, current={Map.GetMapID()}, "
                f"ready={Map.IsMapReady()})."
            )
        return BehaviorTree.NodeState.RUNNING

    leader_branch = BehaviorTree.SequenceNode(
        name="LeaderTravelToOutpost",
        children=[
            BehaviorTree.ActionNode(
                name="LeaderTravelToOutpostAction",
                action_fn=_leader_travel_action,
                aftercast_ms=2500,
            ),
            BehaviorTree.WaitNode(
                name="LeaderTravelToOutpostWait",
                check_fn=_leader_arrived,
                timeout_ms=leader_travel_timeout_ms,
            ),
        ],
    )

    # ── Phase 2: notify followers to join the leader's district ──────────────
    def _read_leader_travel_payload() -> tuple[int, int, int, int] | None:
        """Read the leader's current map coordinates from the ShMem account
        struct (this is the same data the follower compares against). Falls
        back to the live Map/* readers if the ShMem snapshot isn't ready yet.
        """
        sender_email = Player.GetAccountEmail()
        leader_acc = GLOBAL_CACHE.ShMem.GetAccountDataFromEmail(sender_email)
        if leader_acc is not None:
            try:
                m = leader_acc.AgentData.Map
                return int(m.MapID), int(m.Region), int(m.District), int(m.Language)
            except Exception:
                pass
        try:
            return (
                int(Map.GetMapID()),
                int(Map.GetRegion()[0]),
                int(Map.GetDistrict()),
                int(Map.GetLanguage()[0]),
            )
        except Exception:
            return None

    def _send_travel_to_followers(reason: str) -> int:
        """Send the leader's current map info to every follower. Returns the
        number of messages successfully queued."""
        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]
        if not followers:
            _log("No follower accounts detected; skipping fanout.")
            return 0

        payload = _read_leader_travel_payload()
        if payload is None:
            _log(
                "Leader travel payload could not be read; skipping fanout.",
                Py4GW.Console.MessageType.Warning,
            )
            return 0
        leader_map_id, leader_region, leader_district, leader_language = payload

        sent = 0
        for account in followers:
            target = str(account.AccountEmail)
            try:
                acc_iso = bool(getattr(account, "IsIsolated", False))
            except Exception:
                acc_iso = False
            if acc_iso:
                _log(
                    f"Follower {target} has IsIsolated=True; ShMem will reject "
                    f"the message. Disable account isolation for this follower.",
                    Py4GW.Console.MessageType.Warning,
                )

            _dlog(
                f"  -> TravelToMap [{reason}] to {target} "
                f"(map={leader_map_id}, region={leader_region}, "
                f"district={leader_district}, lang={leader_language})"
            )
            msg_idx = GLOBAL_CACHE.ShMem.SendMessage(
                sender_email,
                target,
                SharedCommandType.TravelToMap,
                (
                    float(leader_map_id),
                    float(leader_region),
                    float(leader_district),
                    float(leader_language),
                ),
            )
            _dlog(f"     SendMessage returned slot={msg_idx}")
            if msg_idx == -1:
                _log(
                    f"SendMessage to {target} failed (slot exhaustion or "
                    f"isolation block).",
                    Py4GW.Console.MessageType.Warning,
                )
            else:
                sent += 1
        _log(
            f"[{reason}] Dispatched TravelToMap to {sent}/{len(followers)} "
            f"follower(s) -> map {leader_map_id}, region {leader_region}, "
            f"district {leader_district}, language {leader_language}."
        )
        return sent

    state["last_redispatch_tick"] = 0

    def _notify_followers() -> BehaviorTree.NodeState:
        _send_travel_to_followers("initial")
        state["last_redispatch_tick"] = int(Py4GW.Game.get_tick_count64())
        return BehaviorTree.NodeState.SUCCESS

    notify_branch = BehaviorTree.ActionNode(
        name="NotifyFollowersTravelToOutpost",
        action_fn=_notify_followers,
        aftercast_ms=follower_dispatch_settle_ms,
    )

    # ── Phase 3: wait until every account is in leader's map+district ────────
    _REDISPATCH_INTERVAL_MS = 10_000

    def _all_accounts_synced() -> BehaviorTree.NodeState:
        leader_map_id   = int(Map.GetMapID())
        leader_district = int(Map.GetDistrict())
        if leader_map_id <= 0:
            return BehaviorTree.NodeState.RUNNING

        accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        if not accounts:
            return BehaviorTree.NodeState.SUCCESS

        pending: list[str] = []
        for acc in accounts:
            try:
                acc_map = int(acc.AgentData.Map.MapID)
                acc_district = int(acc.AgentData.Map.District)
            except Exception:
                return BehaviorTree.NodeState.RUNNING
            if acc_map != leader_map_id or acc_district != leader_district:
                pending.append(
                    f"{acc.AccountEmail}(map={acc_map},district={acc_district})"
                )

        if not pending:
            _log(
                f"All accounts arrived in map {leader_map_id} "
                f"on district {leader_district}."
            )
            return BehaviorTree.NodeState.SUCCESS

        # Log only when the pending set changes.
        pending_key = tuple(sorted(pending))
        if state.get("outpost_sync_pending") != pending_key:
            state["outpost_sync_pending"] = pending_key
            _dlog(
                f"Outpost sync waiting on (target map={leader_map_id}, "
                f"district={leader_district}): {list(pending_key)}"
            )

        # Periodic re-dispatch in case the first message was lost / unprocessed.
        now = int(Py4GW.Game.get_tick_count64())
        if now - int(state.get("last_redispatch_tick") or 0) >= _REDISPATCH_INTERVAL_MS:
            state["last_redispatch_tick"] = now
            _send_travel_to_followers("redispatch")

        return BehaviorTree.NodeState.RUNNING

    sync_branch = BehaviorTree.WaitNode(
        name="WaitAllAccountsInOutpost",
        check_fn=_all_accounts_synced,
        timeout_ms=follower_sync_timeout_ms,
    )

    root = BehaviorTree.SequenceNode(
        name="TravelLeaderToOutpostThenFollowers",
        children=[
            leader_branch,
            notify_branch,
            sync_branch,
        ],
    )

    return BehaviorTree(root)

# ╔══════════════════════════════════════════════════════════════════
# ║                    PARTY HELPERS
# ╚══════════════════════════════════════════════════════════════════

def InviteAllAccountsToParty(
    invite_dispatch_settle_ms: int = 750,
    sync_timeout_ms: int = 30_000,
) -> BehaviorTree:
    """Invite every other running account into the leader's party.

    Behavior:
      1. The leader iterates over every other account that shares its current
         map + region + district + language and is not already in the same
         party. For each match it both
           * invites locally via ``GLOBAL_CACHE.Party.Players.InvitePlayer``
             (so the leader becomes the host), and
           * sends a ``SharedCommandType.InviteToParty`` shared-memory message
             so the receiver also fires the invite back (this is what the
             multibox helpers in Py4GWCoreLib do — both sides invite, which
             auto-joins under multibox).
      2. Waits until every account in the same district reports the leader's
         ``PartyID`` (or until ``sync_timeout_ms`` elapses).

    Returns SUCCESS once the synchronization step finishes (or times out).
    """

    state: dict = {}

    def _dispatch_invites() -> BehaviorTree.NodeState:
        # Reuse HeroAI's existing "Form Party" / "Invite all heroes to party"
        # command so we get the proven invite flow (priority order, local
        # `/invite <name>`, ShMem fanout) for free.
        try:
            from HeroAI.commands import HeroAICommands
        except Exception as exc:
            _log(
                f"InviteAllAccountsToParty: failed to load HeroAI.commands "
                f"({exc}); aborting invite step.",
                Py4GW.Console.MessageType.Error,
            )
            return BehaviorTree.NodeState.FAILURE

        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        targets = [a for a in all_accounts if a.AccountEmail != sender_email]
        _dlog(
            f"InviteAllAccountsToParty: leader={sender_email}, "
            f"party_id={int(Party.GetPartyID())}, "
            f"targets={len(targets)}"
        )
        if not targets:
            _log("Leader: no eligible follower accounts to invite.")
            return BehaviorTree.NodeState.SUCCESS

        try:
            HeroAICommands().FormParty(targets)
        except Exception as exc:
            _log(
                f"InviteAllAccountsToParty: HeroAICommands.FormParty failed: {exc}",
                Py4GW.Console.MessageType.Error,
            )
            return BehaviorTree.NodeState.FAILURE

        _log(f"Leader: dispatched HeroAI 'Form Party' to {len(targets)} account(s).")
        return BehaviorTree.NodeState.SUCCESS

    invite_branch = BehaviorTree.ActionNode(
        name="DispatchPartyInvites",
        action_fn=_dispatch_invites,
        aftercast_ms=invite_dispatch_settle_ms,
    )

    def _all_in_party() -> BehaviorTree.NodeState:
        leader_map_id   = int(Map.GetMapID())
        leader_district = int(Map.GetDistrict())
        leader_party_id = int(Party.GetPartyID())
        if leader_party_id <= 0:
            return BehaviorTree.NodeState.RUNNING

        accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        if not accounts:
            return BehaviorTree.NodeState.SUCCESS

        sender_email = Player.GetAccountEmail()
        pending: list[str] = []
        for account in accounts:
            if account.AccountEmail == sender_email:
                continue
            try:
                acc_map      = int(account.AgentData.Map.MapID)
                acc_district = int(account.AgentData.Map.District)
                acc_party_id = int(account.AgentPartyData.PartyID)
            except Exception:
                return BehaviorTree.NodeState.RUNNING
            # Only require accounts that are actually with the leader.
            if acc_map != leader_map_id or acc_district != leader_district:
                continue
            if acc_party_id != leader_party_id:
                pending.append(f"{account.AccountEmail}(party_id={acc_party_id})")

        if not pending:
            _log(f"All accounts joined leader's party (id {leader_party_id}).")
            return BehaviorTree.NodeState.SUCCESS

        pending_key = tuple(sorted(pending))
        if state.get("party_sync_pending") != pending_key:
            state["party_sync_pending"] = pending_key
            _dlog(
                f"Party sync waiting (leader party_id={leader_party_id}): "
                f"{list(pending_key)}"
            )
        return BehaviorTree.NodeState.RUNNING

    sync_branch = BehaviorTree.WaitNode(
        name="WaitAllAccountsInParty",
        check_fn=_all_in_party,
        timeout_ms=sync_timeout_ms,
    )

    root = BehaviorTree.SequenceNode(
        name="InviteAllAccountsToParty",
        children=[invite_branch, sync_branch],
    )

    return BehaviorTree(root)


# ╔══════════════════════════════════════════════════════════════════
# ║                    MERCHANT RULES HELPERS
# ╚══════════════════════════════════════════════════════════════════

# Opcodes mirrored from Widgets/Guild Wars/Items & Loot/MerchantRules.py
_MERCHANT_RULES_OPCODE_PREVIEW = 2
_MERCHANT_RULES_OPCODE_EXECUTE = 3


def _get_merchant_rules_widget():
    """Return the MerchantRules WIDGET_INSTANCE via the widget handler, or None."""
    try:
        widget_handler = get_widget_handler()
        for widget_name in ("MerchantRules", "Merchant Rules"):
            widget_info = widget_handler.get_widget_info(widget_name)
            if not widget_info or not getattr(widget_info, "module", None):
                continue
            instance = getattr(widget_info.module, "WIDGET_INSTANCE", None)
            if instance is not None:
                return instance
    except Exception:
        pass
    return None


def RunMerchantRulesOnAllAccounts(
    execute_timeout_ms: int = 180_000,
    follower_timeout_ms: int = 180_000,
    preview_settle_ms: int = 1500,
) -> BehaviorTree:
    """Run MerchantRules ``Preview Plan`` followed by ``Execute Here`` on the
    leader and on every other running account.

    Behavior:
      1. Leader: call ``widget._scan_preview()`` (synchronous).
         Followers: dispatch a ``MerchantRules`` PREVIEW message (opcode 2).
      2. Settle wait so followers can build their preview.
      3. Leader: call ``widget._queue_execute_here()``.
         Followers: dispatch a ``MerchantRules`` EXECUTE message (opcode 3).
      4. Wait until ``widget.execution_running`` is False on the leader and
         every dispatched follower message has been marked as finished
         (mirrors the legacy underworld.py behavior).

    Returns SUCCESS once everyone is done (or the configured timeouts elapse).
    """

    state: dict = {
        "follower_refs": [],
        "execute_dispatched": False,
        "request_id": "",
    }

    # ── Phase 1: dispatch PREVIEW on leader + followers ──────────────────────
    def _dispatch_preview() -> BehaviorTree.NodeState:
        widget = _get_merchant_rules_widget()
        _dlog(f"MerchantRules preview: widget={'<none>' if widget is None else 'ok'}")
        if widget is None:
            _log(
                "MerchantRules widget not available on leader; skipping refill.",
                Py4GW.Console.MessageType.Warning,
            )
            return BehaviorTree.NodeState.FAILURE

        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]

        request_id = f"uw_mr_{int(Py4GW.Console.GetCurrentTimestamp() if hasattr(Py4GW.Console, 'GetCurrentTimestamp') else 0)}"
        state["request_id"] = request_id

        # Leader
        try:
            widget._scan_preview()
            _log("Leader: MerchantRules preview generated.")
        except Exception as exc:
            _log(f"Leader: MerchantRules preview failed: {exc}", Py4GW.Console.MessageType.Warning)

        # Followers
        if followers:
            for account in followers:
                target = str(account.AccountEmail)
                _dlog(f"  -> MR PREVIEW to {target} (request_id={request_id})")
                GLOBAL_CACHE.ShMem.SendMessage(
                    sender_email,
                    target,
                    SharedCommandType.MerchantRules,
                    (float(_MERCHANT_RULES_OPCODE_PREVIEW), 0.0, 0.0, 0.0),
                    (request_id, "Preview", "", ""),
                )
            _log(f"Sent MerchantRules PREVIEW to {len(followers)} follower(s).")
        return BehaviorTree.NodeState.SUCCESS

    preview_branch = BehaviorTree.ActionNode(
        name="MerchantRulesPreviewDispatch",
        action_fn=_dispatch_preview,
        aftercast_ms=preview_settle_ms,
    )

    # ── Phase 2: dispatch EXECUTE on leader + followers ──────────────────────
    def _dispatch_execute() -> BehaviorTree.NodeState:
        if state["execute_dispatched"]:
            return BehaviorTree.NodeState.SUCCESS

        widget = _get_merchant_rules_widget()
        if widget is None:
            return BehaviorTree.NodeState.FAILURE

        sender_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        followers = [a for a in all_accounts if a.AccountEmail != sender_email]

        # Leader
        try:
            widget._queue_execute_here()
            _log("Leader: MerchantRules Execute Here queued.")
        except Exception as exc:
            _log(f"Leader: MerchantRules execute failed: {exc}", Py4GW.Console.MessageType.Warning)

        # Followers
        request_id = state.get("request_id") or "uw_mr_exec"
        sent_refs: list[tuple[str, int]] = []
        for account in followers:
            target = str(account.AccountEmail)
            _dlog(f"  -> MR EXECUTE to {target} (request_id={request_id})")
            msg_idx = GLOBAL_CACHE.ShMem.SendMessage(
                sender_email,
                target,
                SharedCommandType.MerchantRules,
                (float(_MERCHANT_RULES_OPCODE_EXECUTE), 0.0, 0.0, 0.0),
                (request_id, "Execute", "", ""),
            )
            if msg_idx != -1:
                sent_refs.append((target, int(msg_idx)))

        if sent_refs:
            _log(f"Sent MerchantRules EXECUTE to {len(sent_refs)} follower(s).")
        state["follower_refs"] = sent_refs
        state["execute_dispatched"] = True
        return BehaviorTree.NodeState.SUCCESS

    execute_branch = BehaviorTree.ActionNode(
        name="MerchantRulesExecuteDispatch",
        action_fn=_dispatch_execute,
        aftercast_ms=500,
    )

    # ── Phase 3: wait for everyone to finish ─────────────────────────────────
    def _all_done() -> BehaviorTree.NodeState:
        widget = _get_merchant_rules_widget()
        leader_busy = bool(widget is not None and getattr(widget, "execution_running", False))

        # Inline replication of outbound_messages_done so we don't pull in the
        # combat_engine recipe layer just for this single check.
        sender_email = Player.GetAccountEmail()
        pending: list[str] = []
        for account_email, message_index in state.get("follower_refs", []):
            if message_index < 0:
                continue
            message = GLOBAL_CACHE.ShMem.GetInbox(message_index)
            still_active = (
                bool(getattr(message, "Active", False))
                and str(getattr(message, "ReceiverEmail", "") or "") == account_email
                and str(getattr(message, "SenderEmail", "") or "") == sender_email
                and int(getattr(message, "Command", -1)) == int(SharedCommandType.MerchantRules)
            )
            if still_active:
                pending.append(f"{account_email}(msg#{message_index})")

        if not leader_busy and not pending:
            _log("MerchantRules: leader and followers finished.")
            return BehaviorTree.NodeState.SUCCESS

        # Log only when the (leader_busy, pending) status changes.
        status_key = (leader_busy, tuple(sorted(pending)))
        if state.get("mr_sync_status") != status_key:
            state["mr_sync_status"] = status_key
            _dlog(
                f"MerchantRules sync: leader_busy={leader_busy}, "
                f"pending_followers={list(status_key[1])}"
            )
        return BehaviorTree.NodeState.RUNNING

    sync_branch = BehaviorTree.WaitNode(
        name="MerchantRulesWaitAllAccounts",
        check_fn=_all_done,
        timeout_ms=max(execute_timeout_ms, follower_timeout_ms),
    )

    root = BehaviorTree.SequenceNode(
        name="RunMerchantRulesOnAllAccounts",
        children=[
            preview_branch,
            execute_branch,
            sync_branch,
        ],
    )

    return BehaviorTree(root)


# ╔══════════════════════════════════════════════════════════════════
# ║                    PASSAGE SCROLL HELPERS
# ╚══════════════════════════════════════════════════════════════════

# Reusable INI file used as a transport for the cross-account inventory
# query (the receiver writes its count back into this INI; we poll it).
_PASSAGE_SCROLL_INI_REL_PATH = os.path.join("Widgets", "Config", "BottingTreeFunctions.ini")
_PASSAGE_SCROLL_INI_SECTION  = "PassageScrollCounts"


def _passage_scroll_ini() -> IniHandler:
    abs_path = os.path.join(Py4GW.Console.get_projects_path(), _PASSAGE_SCROLL_INI_REL_PATH)
    return IniHandler(abs_path)


def _account_ini_key(email: str) -> str:
    """INI keys can't contain '@' / '+' robustly across handlers; normalize."""
    return email.replace("@", "_at_").replace("+", "_plus_").replace(".", "_")


def UsePassageScrollOnRandomAccount(
    scroll_model_id: int,
    target_map_id: int,
    query_timeout_ms: int = 10_000,
    arrival_timeout_ms: int = 60_000,
    scroll_label: str = "Passage Scroll",
) -> BehaviorTree:
    """Count ``scroll_model_id`` across the whole multibox team, log the
    breakdown, then use the scroll on a randomly-picked account that has at
    least one — pulling the entire party into ``target_map_id``.

    Behavior:
      1. Leader counts its own copies via ``Inventory.GetModelCount``.
         For every other running account it sends an ``InventoryQuery`` via
         ShMem; the receiver writes the count into a shared INI key.
      2. Polls the INI until every account has reported (or the query
         times out). Logs a per-account breakdown plus the team total.
      3. If at least one account has the scroll, picks one at random from the
         eligible set. If the leader is picked, uses the scroll locally;
         otherwise dispatches a ``SharedCommandType.UseItem`` to that
         follower so it consumes its own copy.
      4. Waits up to ``arrival_timeout_ms`` for the leader to land in
         ``target_map_id``.

    Returns FAILURE only if no scroll is found anywhere on the team.
    """

    state: dict = {
        "leader_email": "",
        "follower_emails": [],
        "scroll_counts": {},          # email -> int (-1 == still pending)
        "query_started_tick": 0,
        "chosen_email": "",
        "scroll_used": False,
    }

    # ── Phase 1: dispatch the inventory query ────────────────────────────────
    def _kickoff_query() -> BehaviorTree.NodeState:
        leader_email = Player.GetAccountEmail()
        all_accounts = GLOBAL_CACHE.ShMem.GetAllAccountData() or []
        follower_emails = [
            str(a.AccountEmail) for a in all_accounts if a.AccountEmail != leader_email
        ]

        state["leader_email"] = leader_email
        state["follower_emails"] = follower_emails
        state["scroll_counts"] = {leader_email: int(GLOBAL_CACHE.Inventory.GetModelCount(scroll_model_id))}
        state["query_started_tick"] = int(Py4GW.Game.get_tick_count64())
        state["chosen_email"] = ""
        state["scroll_used"] = False

        ini = _passage_scroll_ini()
        for email in follower_emails:
            ini.write_key(_PASSAGE_SCROLL_INI_SECTION, _account_ini_key(email), str(-1))
            state["scroll_counts"][email] = -1

        _log(
            f"{scroll_label} query: leader has "
            f"{state['scroll_counts'][leader_email]} copies of model "
            f"{scroll_model_id}; querying {len(follower_emails)} follower(s)."
        )
        for email in follower_emails:
            _dlog(f"  -> InventoryQuery '{scroll_label}' to {email}")
            msg_idx = GLOBAL_CACHE.ShMem.SendMessage(
                leader_email,
                email,
                SharedCommandType.InventoryQuery,
                (float(scroll_model_id), float(scroll_model_id), 0.0, 0.0),
                (
                    "report_inventory_count",
                    _PASSAGE_SCROLL_INI_REL_PATH,
                    _PASSAGE_SCROLL_INI_SECTION,
                    _account_ini_key(email),
                ),
            )
            _dlog(f"     SendMessage returned slot={msg_idx}")
        return BehaviorTree.NodeState.SUCCESS

    kickoff_branch = BehaviorTree.ActionNode(
        name=f"InventoryQuery_{scroll_label.replace(' ', '')}",
        action_fn=_kickoff_query,
        aftercast_ms=200,
    )

    # ── Phase 2: poll INI until every account reported (or timeout) ──────────
    def _poll_responses() -> BehaviorTree.NodeState:
        ini = _passage_scroll_ini()
        pending: list[str] = []
        for email in state["follower_emails"]:
            if state["scroll_counts"].get(email, -1) >= 0:
                continue
            count = ini.read_int(
                _PASSAGE_SCROLL_INI_SECTION, _account_ini_key(email), -1
            )
            if count >= 0:
                state["scroll_counts"][email] = count
                _dlog(f"  query response from {email}: {count}")
            else:
                pending.append(email)

        elapsed = int(Py4GW.Game.get_tick_count64()) - int(state["query_started_tick"])
        if not pending or elapsed >= int(query_timeout_ms):
            if pending:
                _log(
                    f"{scroll_label} query timed out for {len(pending)} "
                    f"account(s): {pending}",
                    Py4GW.Console.MessageType.Warning,
                )
                # Treat unresponsive accounts as having zero copies.
                for email in pending:
                    state["scroll_counts"][email] = 0

            counts = state["scroll_counts"]
            total = sum(max(0, int(c)) for c in counts.values())
            breakdown = ", ".join(
                f"{email}={int(counts[email])}"
                for email in sorted(counts.keys())
            )
            _log(f"{scroll_label}: team has {total} copies total. [{breakdown}]")
            return BehaviorTree.NodeState.SUCCESS

        # Log only when the pending set changes.
        pending_key = tuple(sorted(pending))
        if state.get("query_pending_key") != pending_key:
            state["query_pending_key"] = pending_key
            _dlog(
                f"{scroll_label} query waiting on {len(pending)} account(s)."
            )
        return BehaviorTree.NodeState.RUNNING

    poll_branch = BehaviorTree.WaitNode(
        name=f"InventoryQueryWait_{scroll_label.replace(' ', '')}",
        check_fn=_poll_responses,
        timeout_ms=int(query_timeout_ms) + 5_000,
    )

    # ── Phase 3: pick one account and trigger the scroll usage ───────────────
    def _use_scroll() -> BehaviorTree.NodeState:
        counts = state["scroll_counts"]
        eligible = [email for email, count in counts.items() if int(count) > 0]
        if not eligible:
            _log(
                f"{scroll_label}: no account has any copies; cannot enter map "
                f"{target_map_id}.",
                Py4GW.Console.MessageType.Error,
            )
            return BehaviorTree.NodeState.FAILURE

        leader_email = state["leader_email"]

        # Prefer the leader whenever it has copies: this matches the legacy
        # underworld.py behavior (handle_use_item -> Inventory.UseItem locally)
        # and avoids the receiver-side `team_consume_opt_in` Pycons gate that
        # silently drops cross-account UseItem messages.
        if int(counts.get(leader_email, 0)) > 0:
            chosen = leader_email
        else:
            chosen = random.choice(eligible)
        state["chosen_email"] = chosen
        _log(
            f"{scroll_label}: using scroll on {chosen} "
            f"({int(counts[chosen])} available)."
        )

        if chosen == leader_email:
            # Local invocation matches Sources/modular_bot/recipes/
            # actions_interaction.handle_use_item.
            item_id = int(GLOBAL_CACHE.Inventory.GetFirstModelID(int(scroll_model_id)))
            if item_id <= 0:
                _log(
                    f"{scroll_label}: leader could not resolve item id for model "
                    f"{scroll_model_id} (count={int(counts[leader_email])}).",
                    Py4GW.Console.MessageType.Error,
                )
                return BehaviorTree.NodeState.FAILURE
            GLOBAL_CACHE.Inventory.UseItem(item_id)
            _dlog(f"  local UseItem(item_id={item_id}, model={scroll_model_id})")
        else:
            # Follower path: requires receiver-side Pycons opt-in
            # (`team_consume_opt_in = True`). If the message is silently dropped,
            # the arrival WaitNode below will time out.
            msg_idx = GLOBAL_CACHE.ShMem.SendMessage(
                leader_email,
                chosen,
                SharedCommandType.UseItem,
                (float(scroll_model_id), 1.0, 0.0, 0.0),
            )
            _dlog(
                f"  -> UseItem to {chosen} (model={scroll_model_id}) "
                f"returned slot={msg_idx}"
            )
            if msg_idx == -1:
                _log(
                    f"{scroll_label}: SendMessage to {chosen} failed.",
                    Py4GW.Console.MessageType.Error,
                )
                return BehaviorTree.NodeState.FAILURE
            _log(
                f"{scroll_label}: follower UseItem dispatched. Note that the "
                f"follower must have Pycons 'team_consume_opt_in' enabled or "
                f"the scroll will be ignored silently.",
                Py4GW.Console.MessageType.Warning,
            )

        state["scroll_used"] = True
        return BehaviorTree.NodeState.SUCCESS

    use_branch = BehaviorTree.ActionNode(
        name=f"UseScroll_{scroll_label.replace(' ', '')}",
        action_fn=_use_scroll,
        aftercast_ms=2_000,
    )

    # ── Phase 4: wait for leader to land in target map ───────────────────────
    def _leader_in_target() -> BehaviorTree.NodeState:
        if int(Map.GetMapID()) == int(target_map_id) and Map.IsMapReady():
            _log(f"{scroll_label}: leader arrived in map {target_map_id}.")
            return BehaviorTree.NodeState.SUCCESS
        if not state.get("arrival_wait_logged"):
            state["arrival_wait_logged"] = True
            _dlog(
                f"{scroll_label}: waiting for leader to land in map "
                f"{target_map_id} (current={Map.GetMapID()})."
            )
        return BehaviorTree.NodeState.RUNNING

    arrival_branch = BehaviorTree.WaitNode(
        name=f"WaitLeaderArriveAt_{target_map_id}",
        check_fn=_leader_in_target,
        timeout_ms=int(arrival_timeout_ms),
    )

    root = BehaviorTree.SequenceNode(
        name="UsePassageScrollOnRandomAccount",
        children=[
            kickoff_branch,
            poll_branch,
            use_branch,
            arrival_branch,
        ],
    )

    return BehaviorTree(root)

