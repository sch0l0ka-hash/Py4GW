MODULE_NAME = "Dead Enemy Target Test"

import Py4GW
import PyImGui
from Py4GWCoreLib import AgentArray, Player, Routines
from Py4GWCoreLib.Agent import Agent

# --- State ---
_target_id: int = 0
_target_locked: bool = False
_log: list[str] = []

MAX_LOG = 20


def _push_log(msg: str) -> None:
    _log.append(msg)
    if len(_log) > MAX_LOG:
        _log.pop(0)
    Py4GW.Console.Log(MODULE_NAME, msg, Py4GW.Console.MessageType.Info)


def _find_nearest_dead_enemy() -> int:
    player_pos = Player.GetXY()
    all_agents = AgentArray.GetAgentArray()
    dead_enemies = [
        a for a in all_agents
        if Agent.IsValid(a)
        and Agent.IsDead(a)
        and Agent.GetAllegiance(a)[1] == "Enemy"
    ]
    if not dead_enemies:
        return 0
    dead_enemies = AgentArray.Sort.ByDistance(dead_enemies, player_pos)
    return int(dead_enemies[0])


def _describe(agent_id: int) -> str:
    valid    = Agent.IsValid(agent_id)
    dead     = Agent.IsDead(agent_id) if valid else "n/a"
    spawned  = Agent.IsSpawned(agent_id) if valid else "n/a"
    hp       = f"{Agent.GetHealth(agent_id):.3f}" if valid else "n/a"
    return f"id={agent_id}  valid={valid}  dead={dead}  spawned={spawned}  hp={hp}"


def configure():
    pass


def main():
    global _target_id, _target_locked

    PyImGui.begin(MODULE_NAME)

    # --- Lock/Unlock controls ---
    if _target_locked:
        if PyImGui.button("Unlock target"):
            _target_locked = False
            _target_id = 0
            _push_log("Target unlocked.")
    else:
        if PyImGui.button("Lock nearest dead enemy"):
            found = _find_nearest_dead_enemy()
            if found:
                _target_id = found
                _target_locked = True
                Player.ChangeTarget(_target_id)
                _push_log(f"Locked: {_describe(_target_id)}")
            else:
                _push_log("No dead enemy nearby.")

    PyImGui.separator()

    # --- Status display ---
    if _target_locked and _target_id:
        desc = _describe(_target_id)
        PyImGui.text(f"Watching: {desc}")

        # Keep re-targeting every frame so GW doesn't drop it
        current_target = Player.GetTargetID() if hasattr(Player, "GetTargetID") else 0
        if current_target != _target_id:
            try:
                Player.ChangeTarget(_target_id)
            except Exception as e:
                _push_log(f"ChangeTarget raised: {e}")

        # Detect despawn
        if not Agent.IsValid(_target_id):
            _push_log(f"DESPAWNED: id={_target_id} is no longer valid!")
            _target_locked = False
            _target_id = 0
        elif not Agent.IsSpawned(_target_id):
            _push_log(f"NOT SPAWNED: id={_target_id} IsSpawned=False")
    else:
        PyImGui.text("No target locked.")

    PyImGui.separator()
    PyImGui.text("Log:")
    for line in reversed(_log):
        PyImGui.text(line)

    PyImGui.end()
