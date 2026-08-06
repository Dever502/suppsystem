"""Stable public facade for subscription panel use cases."""

from resolvate.panel_action_service import PanelActionService
from resolvate.panel_types import Mutation as Mutation
from resolvate.panel_types import PanelActionResult as PanelActionResult
from resolvate.panel_types import PanelActionStatus as PanelActionStatus
from resolvate.panel_types import PanelLookupStatus as PanelLookupStatus
from resolvate.panel_types import PanelSubscriptionInfo as PanelSubscriptionInfo
from resolvate.panel_types import PanelSubscriptionLookup as PanelSubscriptionLookup
from resolvate.panel_types import RemnawaveOperator as RemnawaveOperator
from resolvate.panel_types import RemnawaveReader as RemnawaveReader
from resolvate.panel_types import subscription_info as subscription_info


class PanelService(PanelActionService):
    """Complete Remnawave lookup, mutation and recovery facade."""
