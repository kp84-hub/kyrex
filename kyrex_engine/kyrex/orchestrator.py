import sys
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field

# ==========================================
# 1. DATA STRUCTURES & CONTRACTS
# ==========================================

class ExecutionStep(BaseModel):
    target_file: str = Field(..., description="The workspace file being targeted.")
    action: str = Field(..., description="The tool or action type (e.g., patch, write, shell).")
    description: str = Field(..., description="Human-readable description of what this step achieves.")
    payload: dict = Field(default_factory=dict, description="Arguments/code snippets for tool execution.")

class ExecutionPlan(BaseModel):
    steps: List[ExecutionStep] = Field(default_factory=list)
    status: str = "pending"  # pending, approved, rejected, executing
    user_feedback: Optional[str] = None

class CriticResponse(BaseModel):
    is_valid: bool
    feedback: Optional[str] = None

# ==========================================
# 1.5. HISTORY SERIALIZATION (Systems Integrator Fix)
# ==========================================

def format_messages_for_llm(history: List[dict]) -> List[dict]:
    """
    Serializes message history while preserving reasoning_content for models like Kimi K2.6.
    Ensures multi-turn tool calling does not lock up due to missing metadata.
    """
    formatted_messages = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content") or ""
        
        payload = {
            "role": role,
            "content": content
        }
        
        if role == "assistant":
            # CRITICAL: Kimi K2.6 requires this property on historical tool turns
            payload["reasoning_content"] = msg.get("reasoning_content") or msg.get("reasoning") or ""
            
            if msg.get("tool_calls"):
                payload["tool_calls"] = msg.get("tool_calls")
            
            # Moonshot/Kimi validation: assistant message 'content' must not be empty if no tool_calls
            if not payload["content"] and not msg.get("tool_calls"):
                payload["content"] = "..."
        
        elif role == "tool":
            payload["tool_call_id"] = msg.get("tool_call_id")
            
        formatted_messages.append(payload)
    return formatted_messages

# ==========================================
# 2. INTERACTIVE HUMAN REVIEW GATE
# ==========================================

async def human_review_gate(plan: ExecutionPlan, auto_approve: bool = False) -> ExecutionPlan:
    """
    Blocks execution to present the verified plan to the user.
    Handles manual overrides, feedback loops, and manual plan editing.
    Uses asyncio.to_thread to prevent blocking the event loop during input().
    """
    if auto_approve:
        print("\n\033[94m⏩ [Kyrex] Auto-approve active. Passing plan directly to executor...\033[0m")
        plan.status = "approved"
        return plan

    print("\n" + "\033[1;36m=" * 60)
    print("📋 PROPOSED Kyrex EXECUTION PLAN (Passed Automated Critic)")
    print("=" * 60 + "\033[0m")
    
    for idx, step in enumerate(plan.steps, 1):
        print(f"  \033[1;33m{idx}.\033[0m [\033[1;32m{step.action.upper()}\033[0m] -> \033[1;34m{step.target_file}\033[0m")
        print(f"     Description: {step.description}\n")
    print("\033[1;36m" + "=" * 60 + "\033[0m")

    while True:
        # Using asyncio.to_thread to collect input without blocking the event loop
        prompt = "\033[1m[A]pprove & Run | [R]eject & Refine | [E]dit Plan | [Q]uit:\033[0m "
        choice = (await asyncio.to_thread(input, prompt)).strip().lower()

        if choice == 'a':
            print("\n\033[1;32m🚀 Authorization granted. Commencing tool execution loop...\033[0m")
            plan.status = "approved"
            return plan

        elif choice == 'r':
            feedback_prompt = "\n\033[1m💬 Provide manual steering instructions for the Planner:\033[0m\n> "
            feedback = (await asyncio.to_thread(input, feedback_prompt)).strip()
            if not feedback:
                print("\033[1;31m❌ Feedback cannot be empty when rejecting.\033[0m")
                continue
            plan.status = "rejected"
            plan.user_feedback = feedback
            return plan

        elif choice == 'e':
            print("\n\033[1;35m🛠️ Interactive step refinement mode:\033[0m")
            for idx, step in enumerate(plan.steps, 1):
                edit_prompt = f" Modify description for step {idx} [\033[3m{step.description}\033[0m]: "
                new_desc = (await asyncio.to_thread(input, edit_prompt)).strip()
                if new_desc:
                    step.description = new_desc
            print("\033[1;32m✅ Plan local mutations applied.\033[0m")
            # Loop continues so they can Approve or Reject the modified plan

        elif choice == 'q':
            print("\n\033[1;31m🛑 Execution aborted by operator. Tearing down session safely.\033[0m")
            sys.exit(0)
        else:
            print("\033[1;31m❌ Invalid selection. Please choose A, R, E, or Q.\033[0m")

# ==========================================
# 3. CORE ORCHESTRATION PIPELINE
# ==========================================

async def run_px_orchestrator(objective: str, context: dict, config: dict, human_gate_callback=None):
    """
    Main state controller implementing the Plan-Review-Gate-Execute lifecycle.
    
    NOTE: This orchestrator pipeline is a structural placeholder.
    The planner, critic, and executor nodes require real LLM-backed implementations.
    Mock stubs have been removed — this function will raise NotImplementedError
    until real node implementations are provided.
    """
    raise NotImplementedError(
        "Orchestrator pipeline requires real planner/critic/executor implementations. "
        "Mock stubs have been removed for code hygiene."
    )
