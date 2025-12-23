import logging
import os
import requests
from datetime import datetime
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, Agent, room_io, AutoSubscribe
from livekit.plugins import openai, noise_cancellation, bey

load_dotenv(".env.local")

def get_memory_string():
    """
    Stub: Load memory from Google Sheets for web debrief.
    TODO: Implement Google Sheets API integration.
    """
    # TODO: Implement Google Sheets API to load call history
    # For now, return empty memory
    return "No known details about the user."

class Assistant(Agent):
    def __init__(self, initial_memory, is_phone) -> None:
        # DYNAMIC PERSONA INJECTION
        # We customize the system prompt based on the source (Phone vs Web)
        
        base_context = f"KNOWN MEMORY:\n{initial_memory}"

        if is_phone:
            # PHONE MODE: The "Gatekeeper"
            # Strict, protective, efficient.
            instructions = f"""
            You are "Sarah", a protective AI Receptionist for James.
            You are currently answering a real phone call (SIP).
            
            YOUR GOALS:
            1. Screen the call. James is busy and didn't answer, so the call was forwarded to you.
            2. Check MEMORY. If the caller is known/vip, put them through (simulate this by being polite).
            3. If the caller is SPAM (car warranty, insurance, etc.), tell them to remove this number and hang up.
            4. If it is a legitimate lead, ask for their name and message.
            
            {base_context}
            
            Keep responses short (under 2 sentences). Be professional but firm.
            """
        else:
            # WEB MODE: The "Chief of Staff"
            # Visual, analytical, helpful.
            instructions = f"""
            You are "Sarah", James's Chief of Staff.
            You are currently appearing as a 3D Avatar on the web dashboard.
            
            YOUR GOALS:
            1. Welcome James back.
            2. Offer a "Debrief" of recent calls based on the MEMORY logs.
            3. Visualise the data if asked.
            
            {base_context}
            """
        
        super().__init__(instructions=instructions)

server = AgentServer()

@server.rtc_session(agent_name="my-vision-agent")
async def my_agent(ctx: agents.JobContext):
    # 1. SETUP & SOURCE DETECTION
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    
    # Check if this is a phone call by looking for SIP participants
    is_phone = any(p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP for p in ctx.room.remote_participants.values())

    source_log = "PHONE_CALL" if is_phone else "WEB_INTERFACE"
    logging.info(f"Connecting via: {source_log} (Room: {ctx.room.name})")

    # Load memory 
    current_memory = get_memory_string()

    # 2. MODEL CONFIG (OpenAI Mini)
    model = openai.realtime.RealtimeModel(
        model="gpt-4o-mini-realtime-preview-2024-12-17", # The $0.06/min Cost Hook
        voice="coral",
    )

    session = AgentSession(llm=model)

    # 3. CONDITIONAL AVATAR (The Optimization)
    # Only start the Beyond Presence Avatar if we are on the WEB.
    # Phones can't see video, so we save resources here.
    if not is_phone:
        avatar = bey.AvatarSession(
            avatar_id="2bc759ab-a7e5-4b91-941d-9e42450d6546", 
        )
        await avatar.start(session, room=ctx.room)

    # 4. START THE AGENT
    # We pass 'is_phone' to the Assistant so it knows which persona to use.
    await session.start(
        room=ctx.room,
        agent=Assistant(initial_memory=current_memory, is_phone=is_phone),
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                # Use telephony noise cancellation for phone calls
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony() 
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP 
                else noise_cancellation.BVC(),
            ),
        ),
    )

    # 5. SET UP PARTICIPANT DISCONNECT HANDLER
    # Send transcript to n8n when user (SIP participant) hangs up
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        # Only handle SIP participant disconnections (user hangup)
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            payload = {
                "transcript": str(session.llm._chat_ctx.messages),
                "timestamp": datetime.utcnow().isoformat()
            }
            requests.post("https://n8n.n8nsite.live/webhook-test/api/path", json=payload)
    
    ctx.room.on("participant_disconnected", on_participant_disconnected)

    # 6. INITIAL GREETING TRIGGER
    # Triggers the AI to speak first with the correct context
    if is_phone:
        # PHONE MODE (Spam Blocker)
        # We give it the EXACT sentence to read so it never hallucinates.
        await session.generate_reply(
            instructions="Say exactly: 'Hello, this is James's AI. Who is calling?'"
        )
    else:
        # WEB MODE (Chief of Staff)
        await session.generate_reply(
            instructions="Say exactly: 'Welcome back, James. I'm ready for your call debrief.'"
        )
if __name__ == "__main__":
    agents.cli.run_app(server)