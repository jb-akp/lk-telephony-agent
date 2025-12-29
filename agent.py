import json
import logging
import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import AgentServer, AgentSession, Agent, room_io, RunContext, get_job_context, AutoSubscribe
from livekit.agents.llm import function_tool
from livekit.plugins import openai, noise_cancellation, bey

load_dotenv(".env.local")

# Helper function to send transcript to n8n
def send_transcript_to_n8n(session: AgentSession):
    """Send transcript to n8n webhook."""
    try:
        payload = {
            "transcript": json.dumps(session.history.to_dict()["items"]),
            "timestamp": datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()
        }
        response = requests.post(os.getenv("N8N_TRANSCRIPT_WEBHOOK_URL"), json=payload)
        logging.info(f"Transcript sent to n8n. Status: {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Failed to send transcript: {e}")
        return False

# Define tools outside class so they can be conditionally included
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
    
    response = requests.get(os.getenv("N8N_MEMORY_WEBHOOK_URL"))
    memory = response.text if response.status_code == 200 else ""
    
    return memory

@function_tool()
async def hangup_call(run_ctx: RunContext, is_spam: bool = False) -> str:
    """Hang up the call. Use is_spam=True if the caller mentions: car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or any unsolicited sales pitch. Use is_spam=False for normal call endings after collecting the caller's information."""
    logging.info("hangup_call tool executed - spam detected" if is_spam else "hangup_call tool executed - normal hangup")
    run_ctx.disallow_interruptions()
    
    # Different messages based on spam status
    if is_spam:
        message = "You MUST say exactly these words with no changes: 'I'm not interested in unsolicited offers. Please remove this number from your calling list. Goodbye.' Do not add anything else. Do not say 'call has been ended' or offer assistance."
    else:
        message = "Say a polite goodbye, such as: 'Thank you for calling. I'll make sure James gets your message. Have a great day!'"
    
    # Start goodbye message (don't wait for it to finish)
    handle = run_ctx.session.generate_reply(instructions=message)
    
    # Capture transcript data before hanging up
    transcript_data = json.dumps(run_ctx.session.history.to_dict()["items"])
    timestamp = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()
    
    # Hang up immediately by deleting the room
    ctx = get_job_context()
    if ctx is not None:
        logging.info(f"Hanging up call by deleting room: {ctx.room.name}")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
        logging.info(f"Room deleted successfully, call ended")
    
    # Send transcript after hangup (using captured data)
    try:
        payload = {
            "transcript": transcript_data,
            "timestamp": timestamp
        }
        response = requests.post(os.getenv("N8N_TRANSCRIPT_WEBHOOK_URL"), json=payload)
        logging.info(f"Transcript sent to n8n. Status: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to send transcript: {e}")
    
    return ""  # Return value unused since call ends immediately

class Assistant(Agent):
    def __init__(self, is_phone) -> None:
        # Customize persona based on connection source
        if is_phone:
            # Phone mode: Gatekeeper persona
            instructions = """
            You are "Sarah", a protective AI Receptionist for James, answering a phone call forwarded from voicemail.
            PRIORITY: If the caller mentions ANY of these: car warranty, selling a car warranty, extended warranty, insurance offers, debt relief, credit card offers, timeshare, or ANY unsolicited sales pitch, you MUST IMMEDIATELY call the hangup_call function tool with is_spam=True. Do not respond verbally first. Do not ask questions. Just call the tool immediately.
            For legitimate calls: Screen the call, collect name and full message. Let them finish speaking before ending. 
            IMPORTANT: When the caller says goodbye, thanks you, says they're done, or indicates the conversation is complete, you MUST IMMEDIATELY call hangup_call with is_spam=False. Do not continue the conversation after they say goodbye. Just call the tool right away.
            Keep responses under 2 sentences. Be professional, firm, and concise.
            """
            tools = [hangup_call]  # Hangup tool for both spam and normal calls
        else:
            # Web mode: Chief of Staff persona
            instructions = """
            You are "Sarah", James's Chief of Staff, appearing as a 3D Avatar on the web dashboard.
            When asked about voicemails, calls, or call history, use get_call_debrief to retrieve from Google Sheets. If data exists, summarize it. If empty, say "I don't see any recent calls yet." Never invent call information.
            Keep responses concise (1-2 sentences unless detail is requested). Welcome James back and offer help.
            """
            tools = [get_call_debrief]  # Include tool for web mode
        
        super().__init__(instructions=instructions, tools=tools)

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    # Connect to room first to see participants
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Detect if this is a phone call (SIP participant)
    # Primary: check room name pattern (SIP calls have phone numbers in room name - always available immediately)
    is_phone = ctx.room.name.startswith("call-") and "+" in ctx.room.name
    
    # Secondary: verify by checking for SIP participant (may not be available immediately)
    if not is_phone:
        for participant in ctx.room.remote_participants.values():
            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                is_phone = True
                break
    
    logging.info(f"is_phone: {is_phone}, room: {ctx.room.name}")

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-4o-mini-realtime-preview-2024-12-17",
            voice="coral",
        )
    )

    # Start avatar only for web (phones can't see video)
    if not is_phone:
        avatar = bey.AvatarSession(
            avatar_id="2bc759ab-a7e5-4b91-941d-9e42450d6546", 
        )
        await avatar.start(session, room=ctx.room)

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

    if is_phone:
        await session.generate_reply(
            instructions="Say: 'Hello, this is James's AI. Who is calling?'"
        )
    else:
        await session.generate_reply(
            instructions="Say: 'Welcome back, James. How can I help you today?'"
        )
    
if __name__ == "__main__":
    agents.cli.run_app(server)