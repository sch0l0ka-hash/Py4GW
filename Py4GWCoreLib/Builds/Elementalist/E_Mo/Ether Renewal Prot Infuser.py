import time

from Py4GWCoreLib import AgentArray, GLOBAL_CACHE, Profession, Range, Routines
from Py4GWCoreLib.Agent import Agent
from Py4GWCoreLib.Builds.Any.HeroAI import HeroAI_Build
from Py4GWCoreLib import BuildMgr
from Py4GWCoreLib.Player import Player
from Py4GWCoreLib.Skill import Skill
from Py4GWCoreLib.Builds.Skills import SkillsTemplate


Aura_of_Restoration_ID = Skill.GetID("Aura_of_Restoration")
Ether_Renewal_ID = Skill.GetID("Ether_Renewal")
Protective_Spirit_ID = Skill.GetID("Protective_Spirit")
Reversal_of_Fortune_ID = Skill.GetID("Reversal_of_Fortune")
Breath_of_the_Great_Dwarf_ID = Skill.GetID("Breath_of_the_Great_Dwarf")
Great_Dwarf_Weapon_ID = Skill.GetID("Great_Dwarf_Weapon")
Vital_Blessing_ID = Skill.GetID("Vital_Blessing")
Life_Attunement_ID = Skill.GetID("Life_Attunement")
Infuse_Health_ID = Skill.GetID("Infuse_Health")
Spirit_Bond_ID = Skill.GetID("Spirit_Bond")
Protective_Bond_ID = Skill.GetID("Protective_Bond")
Burning_Speed_ID = Skill.GetID("Burning_Speed")

LOW_ENERGY_ENTER_THRESHOLD = 0.70
LOW_ENERGY_EXIT_THRESHOLD = 0.70
MIN_BOND_ENERGY_PIPS = -9


class Ether_Renewal_Prot_Infuser(BuildMgr):
    def __init__(self, match_only: bool = False):
        super().__init__(
            name="Ether Renewal Prot Infuser",
            required_primary=Profession.Elementalist,
            required_secondary=Profession.Monk,
            template_code="OgNDwaTPHzse1iWAAAAAAA",
            required_skills=[
                Aura_of_Restoration_ID,
                Ether_Renewal_ID,
                Infuse_Health_ID,
            ],
            optional_skills=[
                Protective_Spirit_ID,
                Reversal_of_Fortune_ID,
                Spirit_Bond_ID,
                Protective_Bond_ID,
                Life_Attunement_ID,
                Burning_Speed_ID,
                Breath_of_the_Great_Dwarf_ID,
                Great_Dwarf_Weapon_ID,
                Vital_Blessing_ID,
            ],
        )

        if match_only:
            return

        self.SetFallback("HeroAI", HeroAI_Build(standalone_fallback=True))
        self.SetSkillCastingFn(self._run_local_skill_logic)
        self.skills: SkillsTemplate = SkillsTemplate(self)
        self._energy_recovery_mode = False
        self._cast_movement_pause_until_ms = 0.0
        self._cast_movement_pause_skill_id = 0

    def _get_enchantment_cast_block_ms(self, skill_id: int) -> int:
        activation_ms = int(max(0.0, GLOBAL_CACHE.Skill.Data.GetActivation(skill_id)) * 1000)
        return max(activation_ms + 250, 2250)

    def _mark_cast_movement_pause(self, skill_id: int, cast_block_ms: int) -> None:
        self._cast_movement_pause_skill_id = int(skill_id)
        self._cast_movement_pause_until_ms = time.monotonic() * 1000.0 + float(cast_block_ms)

    def _sync_cast_movement_pause(self) -> None:
        cached_data = getattr(self, "_cached_data", None)
        if cached_data is None:
            return

        player_id = Player.GetAgentID()
        pause_skill_id = int(self._cast_movement_pause_skill_id or 0)
        now_ms = time.monotonic() * 1000.0
        timer_active = now_ms < float(self._cast_movement_pause_until_ms)
        casting_pause_skill = (
            pause_skill_id != 0
            and Agent.IsCasting(player_id)
            and int(Agent.GetCastingSkillID(player_id) or 0) == pause_skill_id
        )
        cached_data.pause_follow_movement = bool(casting_pause_skill or timer_active)

        if not timer_active and not casting_pause_skill:
            self._cast_movement_pause_skill_id = 0

    def _get_player_energy_pct(self) -> float:
        return float(Agent.GetEnergy(Player.GetAgentID()))

    def _can_maintain_bonds(self) -> bool:
        return int(Agent.GetEnergyPips(Player.GetAgentID())) >= MIN_BOND_ENERGY_PIPS

    def _is_zero_energy(self) -> bool:
        return self._get_player_energy_pct() <= 0.0

    def _player_maintains_bond_on_target(self, skill_id: int, target_agent_id: int) -> bool:
        player_id = Player.GetAgentID()

        for buff in GLOBAL_CACHE.Effects.GetBuffs(player_id):
            if int(buff.skill_id) != skill_id:
                continue
            buff_target = int(getattr(buff, "target_agent_id", 0) or 0)
            if buff_target == target_agent_id:
                return True
            if target_agent_id == player_id and buff_target in (0, player_id):
                return True

        for effect in GLOBAL_CACHE.Effects.GetEffects(player_id):
            if int(effect.skill_id) != skill_id:
                continue
            effect_target = int(getattr(effect, "agent_id", 0) or 0)
            if effect_target == target_agent_id:
                return True
            if target_agent_id == player_id and effect_target in (0, player_id):
                return True

        return Routines.Checks.Effects.HasBuff(target_agent_id, skill_id)

    def _player_maintains_bond(self, skill_id: int) -> bool:
        player_id = Player.GetAgentID()
        for buff in GLOBAL_CACHE.Effects.GetBuffs(player_id):
            if int(buff.skill_id) == skill_id:
                return True
        for effect in GLOBAL_CACHE.Effects.GetEffects(player_id):
            if int(effect.skill_id) == skill_id:
                return True
        return Routines.Checks.Effects.HasBuff(player_id, skill_id)

    def _drop_one_maintained_bond(self, skill_id: int) -> bool:
        player_id = Player.GetAgentID()
        for buff in GLOBAL_CACHE.Effects.GetBuffs(player_id):
            if int(buff.skill_id) == skill_id and int(buff.buff_id) > 0:
                GLOBAL_CACHE.Effects.DropBuff(int(buff.buff_id))
                return True

        if GLOBAL_CACHE.Effects.EffectExists(player_id, skill_id):
            buff_id = int(GLOBAL_CACHE.Effects.GetBuffID(skill_id))
            if buff_id > 0:
                GLOBAL_CACHE.Effects.DropBuff(buff_id)
                return True

        return False

    def _drop_maintained_bonds_at_zero_energy(self) -> bool:
        if not self._is_zero_energy():
            return False

        if self._player_maintains_bond(Life_Attunement_ID) and self._drop_one_maintained_bond(Life_Attunement_ID):
            return True

        if self._player_maintains_bond(Protective_Bond_ID) and self._drop_one_maintained_bond(Protective_Bond_ID):
            return True

        return False

    def _is_low_energy(self) -> bool:
        return self._get_player_energy_pct() < LOW_ENERGY_ENTER_THRESHOLD

    def _update_energy_recovery_mode(self) -> None:
        energy_pct = self._get_player_energy_pct()
        if energy_pct < LOW_ENERGY_ENTER_THRESHOLD:
            self._energy_recovery_mode = True
        elif energy_pct >= LOW_ENERGY_EXIT_THRESHOLD:
            self._energy_recovery_mode = False

    def _is_energy_recovery_mode(self) -> bool:
        return bool(self._energy_recovery_mode)

    def _run_energy_recovery_logic(self):
        player_id = Player.GetAgentID()

        not_has_spirit_bond = lambda: (
            self._can_maintain_bonds()
            and not Routines.Checks.Effects.HasBuff(player_id, Spirit_Bond_ID)
        )
        if (
            self.IsSkillEquipped(Spirit_Bond_ID)
            and not_has_spirit_bond()
            and (yield from self.CastSkillID(
                skill_id=Spirit_Bond_ID,
                extra_condition=not_has_spirit_bond,
                log=False,
                aftercast_delay=250,
                target_agent_id=player_id,
            ))
        ):
            return True

        not_has_aura = lambda: not Routines.Checks.Effects.HasBuff(player_id, Aura_of_Restoration_ID)
        if (yield from self.CastSkillID(
            skill_id=Aura_of_Restoration_ID,
            extra_condition=not_has_aura,
            log=False,
            aftercast_delay=250,
            target_agent_id=player_id,
        )):
            return True

        not_has_reversal_of_fortune = lambda: not Routines.Checks.Effects.HasBuff(player_id, Reversal_of_Fortune_ID)
        if (
            self.IsSkillEquipped(Reversal_of_Fortune_ID)
            and not_has_reversal_of_fortune()
            and (yield from self.CastSkillID(
                skill_id=Reversal_of_Fortune_ID,
                extra_condition=not_has_reversal_of_fortune,
                log=False,
                aftercast_delay=250,
                target_agent_id=player_id,
            ))
        ):
            return True

        if self.IsSkillEquipped(Burning_Speed_ID) and (yield from self._cast_burning_speed_recovery()):
            return True

        return False

    def _cast_burning_speed_recovery(self):
        def _should_cast_burning_speed_recovery() -> bool:
            player_id = Player.GetAgentID()
            return Routines.Checks.Effects.HasBuff(player_id, Ether_Renewal_ID)

        if not self.IsSkillEquipped(Burning_Speed_ID):
            return False
        if not _should_cast_burning_speed_recovery():
            return False

        return (yield from self.CastSkillID(
            skill_id=Burning_Speed_ID,
            extra_condition=_should_cast_burning_speed_recovery,
            log=False,
            aftercast_delay=250,
            target_agent_id=Player.GetAgentID(),
        ))

    def _vital_blessing_self_upkeep(self):
        not_has_vital_blessing = lambda: not Routines.Checks.Effects.HasBuff(Player.GetAgentID(), Vital_Blessing_ID)

        if not self.IsSkillEquipped(Vital_Blessing_ID):
            return False
        if not not_has_vital_blessing():
            return False

        return (yield from self.CastSkillID(
            skill_id=Vital_Blessing_ID,
            extra_condition=not_has_vital_blessing,
            log=False,
            aftercast_delay=250,
            target_agent_id=Player.GetAgentID(),
        ))

    def _life_attunement_self_upkeep(self):
        not_has_life_attunement = lambda: not Routines.Checks.Effects.HasBuff(Player.GetAgentID(), Life_Attunement_ID)

        if not self.IsSkillEquipped(Life_Attunement_ID):
            return False
        if not self._can_maintain_bonds():
            return False
        if not not_has_life_attunement():
            return False

        cast_block_ms = self._get_enchantment_cast_block_ms(Life_Attunement_ID)

        if (yield from self.CastSkillID(
            skill_id=Life_Attunement_ID,
            extra_condition=not_has_life_attunement,
            log=False,
            aftercast_delay=cast_block_ms,
            target_agent_id=Player.GetAgentID(),
        )):
            self._mark_cast_movement_pause(Life_Attunement_ID, cast_block_ms)
            self._sync_cast_movement_pause()
            return True

        return False

    def _resolve_protective_bond_target(self) -> int:
        player_id = Player.GetAgentID()
        if (
            Agent.IsAlive(player_id)
            and not self._player_maintains_bond_on_target(Protective_Bond_ID, player_id)
        ):
            return player_id

        ally_array = Routines.Targeting.GetAllAlliesArray(Range.SafeCompass.value)
        ally_array = AgentArray.Filter.ByCondition(
            ally_array,
            lambda agent_id: Agent.IsAlive(agent_id),
        )
        ally_array = AgentArray.Filter.ByCondition(
            ally_array,
            lambda agent_id: agent_id != player_id,
        )
        ally_array = AgentArray.Filter.ByCondition(
            ally_array,
            lambda agent_id: not self._player_maintains_bond_on_target(Protective_Bond_ID, agent_id),
        )
        ally_array = AgentArray.Filter.ByDistance(ally_array, Player.GetXY(), Range.Spellcast.value)

        ally_array = sorted(list(ally_array or []))
        return ally_array[0] if ally_array else 0

    def _protective_bond_upkeep(self):
        if not self.IsSkillEquipped(Protective_Bond_ID):
            return False
        if not self._can_maintain_bonds():
            return False

        target_agent_id = self._resolve_protective_bond_target()
        if not target_agent_id:
            return False

        cast_block_ms = self._get_enchantment_cast_block_ms(Protective_Bond_ID)
        player_id = Player.GetAgentID()

        if target_agent_id == player_id:
            if (yield from self.CastSkillID(
                skill_id=Protective_Bond_ID,
                log=False,
                aftercast_delay=cast_block_ms,
                target_agent_id=player_id,
            )):
                self._mark_cast_movement_pause(Protective_Bond_ID, cast_block_ms)
                self._sync_cast_movement_pause()
                return True
            return False

        if (yield from self.CastSkillIDAndRestoreTarget(
            skill_id=Protective_Bond_ID,
            target_agent_id=target_agent_id,
            log=False,
            aftercast_delay=cast_block_ms,
        )):
            self._mark_cast_movement_pause(Protective_Bond_ID, cast_block_ms)
            self._sync_cast_movement_pause()
            return True

        return False

    def _cast_infuse_health(self):
        health_threshold = 0.60

        def _can_cast_infuse_health() -> bool:
            player_id = Player.GetAgentID()
            return (
                Agent.GetHealth(player_id) > 0.20
                and Routines.Checks.Effects.HasBuff(player_id, Ether_Renewal_ID)
            )

        def _resolve_infuse_health_target() -> int:
            ally_array = Routines.Targeting.GetAllAlliesArray(Range.Spellcast.value)
            ally_array = AgentArray.Filter.ByCondition(
                ally_array,
                lambda agent_id: Agent.IsAlive(agent_id),
            )
            ally_array = AgentArray.Filter.ByCondition(
                ally_array,
                lambda agent_id: agent_id != Player.GetAgentID(),
            )
            ally_array = AgentArray.Filter.ByCondition(
                ally_array,
                lambda agent_id: Agent.GetHealth(agent_id) < health_threshold,
            )

            ally_array = list(ally_array or [])
            ally_array.sort(key=lambda agent_id: Agent.GetHealth(agent_id))
            return ally_array[0] if ally_array else 0

        if not self.IsSkillEquipped(Infuse_Health_ID):
            return False
        if not _can_cast_infuse_health():
            return False

        target_agent_id = _resolve_infuse_health_target()
        if not target_agent_id:
            return False

        return (yield from self.CastSkillIDAndRestoreTarget(
            skill_id=Infuse_Health_ID,
            target_agent_id=target_agent_id,
            extra_condition=_can_cast_infuse_health,
            log=False,
            aftercast_delay=250,
        ))

    def _cast_burning_speed(self):
        def _should_cast_burning_speed() -> bool:
            player_id = Player.GetAgentID()
            return (
                Routines.Checks.Effects.HasBuff(player_id, Ether_Renewal_ID)
                and (
                    self._is_low_energy()
                    or float(Agent.GetHealth(player_id)) < 0.50
                )
            )

        if not self.IsSkillEquipped(Burning_Speed_ID):
            return False
        if not _should_cast_burning_speed():
            return False

        return (yield from self.CastSkillID(
            skill_id=Burning_Speed_ID,
            extra_condition=_should_cast_burning_speed,
            log=False,
            aftercast_delay=250,
            target_agent_id=Player.GetAgentID(),
        ))

    def _run_local_skill_logic(self):
        if not Routines.Checks.Skills.CanCast():
            self._sync_cast_movement_pause()
            return False

        self._sync_cast_movement_pause()
        self._update_energy_recovery_mode()

        if (yield from self.skills.Elementalist.EnergyStorage.Ether_Renewal()):
            return True

        if self._drop_maintained_bonds_at_zero_energy():
            return True

        if not Routines.Checks.Effects.HasBuff(Player.GetAgentID(), Ether_Renewal_ID):
            return False

        if (yield from self._cast_infuse_health()):
            return True

        if self._is_energy_recovery_mode():
            if (yield from self._run_energy_recovery_logic()):
                return True
            return False

        if (yield from self.skills.Elementalist.EnergyStorage.Aura_of_Restoration()):
            return True

        self.UpdatePartyHealthMonitor(sample_interval_ms=150)

        if self._can_maintain_bonds() and self.IsSkillEquipped(Spirit_Bond_ID) and (yield from self.skills.Monk.ProtectionPrayers.Spirit_Bond(health_threshold=0.90, require_aggro=False)):
            return True

        if self.IsSkillEquipped(Reversal_of_Fortune_ID) and (yield from self.skills.Monk.ProtectionPrayers.Reversal_of_Fortune()):
            return True

        if self.IsSkillEquipped(Protective_Spirit_ID) and (yield from self.skills.Monk.ProtectionPrayers.Protective_Spirit()):
            return True

        if self.IsSkillEquipped(Life_Attunement_ID) and (yield from self._life_attunement_self_upkeep()):
            return True

        if self.IsSkillEquipped(Protective_Bond_ID) and (yield from self._protective_bond_upkeep()):
            return True

        if self.IsSkillEquipped(Burning_Speed_ID) and (yield from self._cast_burning_speed()):
            return True

        if self.IsSkillEquipped(Vital_Blessing_ID) and (yield from self._vital_blessing_self_upkeep()):
            return True

        if self.IsSkillEquipped(Breath_of_the_Great_Dwarf_ID) and (yield from self.skills.Any.NoAttribute.Breath_of_the_Great_Dwarf()):
            return True

        if self.IsSkillEquipped(Great_Dwarf_Weapon_ID) and (yield from self.skills.Any.NoAttribute.Great_Dwarf_Weapon()):
            return True

        return False
