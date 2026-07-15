"""Stable public facade for subscription panel use cases."""

from supportbot.panel_action_service import PanelActionService
from supportbot.panel_types import Mutation as Mutation
from supportbot.panel_types import PanelActionResult as PanelActionResult
from supportbot.panel_types import PanelActionStatus as PanelActionStatus
from supportbot.panel_types import PanelLookupStatus as PanelLookupStatus
from supportbot.panel_types import PanelSubscriptionInfo as PanelSubscriptionInfo
from supportbot.panel_types import PanelSubscriptionLookup as PanelSubscriptionLookup
from supportbot.panel_types import RemnawaveOperator as RemnawaveOperator
from supportbot.panel_types import RemnawaveReader as RemnawaveReader
from supportbot.panel_types import subscription_info

_subscription_info = subscription_info


class PanelService(PanelActionService):
    """Complete Remnawave lookup, mutation and recovery facade."""
