import json
import logging
import os
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, room_io, AutoSubscribe, function_tool, RunContext
from livekit.plugins import openai, noise_cancellation

load_dotenv(".env.local")

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "agent_memory.json")


def get_memory_string():
    """Get memory as a formatted string for injection into agent instructions."""
    if not os.path.exists(MEMORY_FILE):
        return "No known details about the user."
    try:
        with open(MEMORY_FILE, "r") as f:
            data = json.load(f)
    except:
        return "No known details about the user."
    
    if not data:
        return "No known details about the user."
    return "\n".join([f"- {k}: {v}" for k, v in data.items()])

class Assistant(Agent):
    def __init__(self, initial_memory) -> None:
        # 2. INJECT MEMORY INTO PROMPT
        # We load the memory string at startup and inject it into the instructions
        instructions = f"""You are a helpful voice assistant. You have access to long-term memory.

KNOWN USER FACTS:
{initial_memory}
"""
        
        super().__init__(instructions=instructions)
    
    # 1. DEFINE THE TOOL
    # This decorator tells the LLM this function exists
    @function_tool(description="Save a meaningful fact about the user for long-term memory.")
    async def save_user_fact(
        self,
        context: RunContext,  # Required by LiveKit, even if unused
        key: str, 
        value: str
    ) -> str:
        """
        Called when the user asks to remember something or provides a key fact.
        Args:
            key: The category of information (e.g., 'name', 'preference', 'birthday')
            value: The specific detail to remember
        """
        logging.info(f"Saving memory: {key}={value}")
        
        # Load existing memory
        if not os.path.exists(MEMORY_FILE):
            data = {}
        else:
            try:
                with open(MEMORY_FILE, "r") as f:
                    data = json.load(f)
            except:
                data = {}
        
        # Update and save
        data[key] = value
        with open(MEMORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
        
        return f"Memory updated: {key} = {value}"

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Load memory immediately upon connection
    current_memory = get_memory_string()

    model = openai.realtime.RealtimeModel(
        model="gpt-4o-mini-realtime-preview-2024-12-17",
        voice="coral",
    )

    session = AgentSession(
        llm=model,
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(initial_memory=current_memory),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Use telephony noise cancellation for phone calls, regular for other sources
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() 
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP 
                else noise_cancellation.BVC(),
            ),
        ),
    )

    # Determine the source for a custom greeting
    # Logic: If it's a SIP call, it's a "Phone" call. Otherwise, it's "Web".
    is_phone = any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP for p in ctx.room.remote_participants.values())
    source_type = "phone" if is_phone else "web interface"

    await session.generate_reply(
        instructions=f"Greet the user by name if known. Mention that they are connecting via {source_type}."
    )

if __name__ == "__main__":
    agents.cli.run_app(server)