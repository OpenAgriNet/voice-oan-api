from pydantic import BaseModel, Field
from typing import List

class Reasoning(BaseModel):
    """Reasoning for the answer."""
    thinking: List[str] = Field(..., description="The thinking process.")

    def __str__(self) -> str:
        return "<thinking>\n\n".join(self.thinking) + "\n\n</thinking>"
    
class Plan(BaseModel):
    """Create a step-by-step plan to gather information to answer the question."""
    plan: List[str] = Field(..., description="A list of steps to gather information to answer the question.")

    def __str__(self) -> str:
        return "**Plan**:\n\n" + "\n".join([f"{i+1}. {step}" for i, step in enumerate(self.plan)])

def reasoning_tool(reasoning: Reasoning) -> Reasoning:
    """Use this tool to reason about the answer."""
    return reasoning

def planning_tool(plan: Plan) -> Plan:
    """Use this tool to create a step-by-step plan (in English) to gather information to answer the question."""
    return plan