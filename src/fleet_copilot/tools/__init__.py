from fleet_copilot.tools.action_tools import ACTION_TOOL_SPECS
from fleet_copilot.tools.base import ToolContext, ToolOutput, ToolRegistry, ToolSpec
from fleet_copilot.tools.insight_tools import INSIGHT_TOOLS
from fleet_copilot.tools.read_tools import READ_TOOLS


def build_registry() -> ToolRegistry:
    return ToolRegistry(READ_TOOLS + INSIGHT_TOOLS + ACTION_TOOL_SPECS)


__all__ = ["ToolContext", "ToolOutput", "ToolRegistry", "ToolSpec", "build_registry"]
