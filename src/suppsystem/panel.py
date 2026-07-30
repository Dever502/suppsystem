"""Stable public facade for subscription panel use cases."""

from suppsystem.panel_action_service import PanelActionService
from suppsystem.panel_types import Mutation as Mutation
from suppsystem.panel_types import PanelActionResult as PanelActionResult
from suppsystem.panel_types import PanelActionStatus as PanelActionStatus
from suppsystem.panel_types import PanelLookupStatus as PanelLookupStatus
from suppsystem.panel_types import PanelSubscriptionInfo as PanelSubscriptionInfo
from suppsystem.panel_types import PanelSubscriptionLookup as PanelSubscriptionLookup
from suppsystem.panel_types import RemnawaveOperator as RemnawaveOperator
from suppsystem.panel_types import RemnawaveReader as RemnawaveReader
from suppsystem.panel_types import subscription_info as subscription_info


class PanelService(PanelActionService):
    """Complete Remnawave lookup, mutation and recovery facade."""
