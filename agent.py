import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, room_io, RunContext, AutoSubscribe
from livekit.agents.llm import function_tool
from livekit.plugins import openai, noise_cancellation, bey

load_dotenv(".env.local")

# Define tool outside class so it can be conditionally included
@function_tool()
async def get_call_debrief(run_ctx: RunContext) -> str:
    """Retrieve recent call history, voicemail summaries, and debrief information from Google Sheets.
    
    Use this when the user asks about:
    - Voicemails or voicemail summaries
    - Recent calls or call history
    - A debrief or summary of previous conversations
    - What happened in previous phone calls
    
    Returns the actual call history data from Google Sheets. If no calls exist yet, returns an empty string.
    """
    run_ctx.disallow_interruptions()
    
    logging.info("get_call_debrief tool executing...")
    response = requests.get("https://n8n.n8nsite.live/webhook/memory")
    memory = response.text if response.status_code == 200 else ""
    logging.info(f"get_call_debrief tool completed. Status: {response.status_code}, Data length: {len(memory)}")
    
    return memory

class Assistant(Agent):
    def __init__(self, is_phone) -> None:
        # Customize persona based on connection source
        if is_phone:
            # Phone mode: Gatekeeper persona
            instructions = """
            You are "Sarah", a protective AI Receptionist for James.
            You are currently answering a real phone call (SIP) that was forwarded from James's voicemail.
            
            YOUR GOALS:
            1. Screen the call. James is busy and didn't answer, so the call was forwarded to you.
            2. If the caller is SPAM (car warranty, insurance, etc.), tell them to remove this number and hang up.
            3. If it is a legitimate call, collect their name and full message. Let them finish speaking completely before ending the call.
            
            Keep responses short (under 2 sentences). Be professional but firm. Make sure to capture the complete message from the caller.
            Always be concise and direct. Avoid unnecessary elaboration or verbose explanations.
            """
            tools = []  # No tools for phone mode
        else:
            # Web mode: Chief of Staff persona
            instructions = """
            You are "Sarah", James's Chief of Staff.
            You are currently appearing as a 3D Avatar on the web dashboard.
            
            YOUR GOALS:
            1. Welcome James back.
            2. When James asks about voicemails, recent calls, a debrief, call history, or what happened in previous conversations, you MUST use the get_call_debrief function tool to retrieve the information from Google Sheets. First, say "Let me check that for you" and COMPLETE the sentence fully before calling the tool. Do not interrupt your own speech.
            3. After the tool completes:
               - If the tool returns data, summarize it clearly and accurately.
               - If the tool returns empty data or no calls are found, say "I don't see any recent calls in your history yet. Once calls come in, I'll be able to provide you with summaries."
               - NEVER make up or invent call information. Only report what the tool actually returns.
            4. Offer to help with anything else after providing the debrief.
            
            IMPORTANT: Voicemails, calls, and call history all refer to the same thing. Always use the get_call_debrief tool when asked about any of these.
            
            COMMUNICATION STYLE: Always be concise and direct. Keep responses brief and to the point. Avoid lengthy explanations unless specifically asked for detail. For general knowledge questions, provide 1-2 sentences maximum.
            """
            tools = [get_call_debrief]  # Include tool for web mode
        
        super().__init__(instructions=instructions, tools=tools)

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Detect if this is a phone call (SIP participant)
    is_phone = any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP for p in ctx.room.remote_participants.values())

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-mini-realtime-preview-2024-12-17",
            voice="coral",
        )
    )

    # Start avatar only for web (phones can't see video)
    # if not is_phone:
    #     avatar = bey.AvatarSession(
    #         avatar_id="2bc759ab-a7e5-4b91-941d-9e42450d6546", 
    #     )
    #     await avatar.start(session, room=ctx.room)

    # Start agent with appropriate persona
    await session.start(
        room=ctx.room,
        agent=Assistant(is_phone=is_phone),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP else noise_cancellation.BVC(),
            ),
        ),
    )

    # Send transcript to n8n on SIP participant disconnect
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            logging.info("SIP participant disconnected, sending transcript to n8n...")
            payload = {
                "transcript": json.dumps(session.history.to_dict()["items"]),
                "timestamp": datetime.now(timezone(timedelta(hours=-8))).isoformat()
            }
            response = requests.post("https://n8n.n8nsite.live/webhook/api/path", json=payload)
            logging.info(f"Transcript sent to n8n. Status: {response.status_code}")
    
    ctx.room.on("participant_disconnected", on_participant_disconnected)
    
    await session.generate_reply(
        instructions="Welcome back, James. How can I help you today?" if not is_phone 
        else "Say exactly: 'Hello, this is James's AI. Who is calling?'"
    )
    
if __name__ == "__main__":
    agents.cli.run_app(server)