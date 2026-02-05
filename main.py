from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import httpx
from pyairtable import Table
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="AI Receptionist Backend",
    description="Backend service for AI-powered dental clinic receptionist",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Airtable configuration
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

try:
    appointments_table = Table(
        AIRTABLE_API_KEY, 
        AIRTABLE_BASE_ID, 
        os.getenv("AIRTABLE_APPOINTMENTS_TABLE", "Appointments")
    )
    slots_table = Table(
        AIRTABLE_API_KEY, 
        AIRTABLE_BASE_ID, 
        os.getenv("AIRTABLE_SLOTS_TABLE", "Available Slots")
    )
    logs_table = Table(
        AIRTABLE_API_KEY, 
        AIRTABLE_BASE_ID, 
        os.getenv("AIRTABLE_LOGS_TABLE", "Call Logs")
    )
    logger.info("Successfully connected to Airtable")
except Exception as e:
    logger.error(f"Failed to connect to Airtable: {str(e)}")

# Pydantic models
class AvailabilityCheck(BaseModel):
    date: str = Field(..., description="Appointment date in YYYY-MM-DD format")
    time: str = Field(..., description="Appointment time in HH:MM format")
    duration: Optional[int] = Field(30, description="Duration in minutes")

class BookingRequest(BaseModel):
    date: str
    time: str
    patient_name: str
    patient_phone: str
    duration: Optional[int] = 30
    reason: Optional[str] = None
    call_id: Optional[str] = None

class VapiResponse(BaseModel):
    callSid: str
    message: str
    functionResult: Dict

class CallLog(BaseModel):
    call_id: str
    phone_number: str
    transcript: str
    outcome: str
    duration: Optional[int] = None

# Health check
@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "AI Receptionist Backend Running",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat()
    }

# Check availability
@app.post("/check-availability")
async def check_availability(data: AvailabilityCheck):
    """
    Check if an appointment slot is available
    
    Args:
        data: AvailabilityCheck object with date, time, and duration
        
    Returns:
        JSON with availability status and slot details
    """
    try:
        logger.info(f"Checking availability for {data.date} at {data.time}")
        
        # Query Airtable for available slot
        formula = f"AND({{Date}}='{data.date}', {{Time}}='{data.time}', {{Booked}}=FALSE())"
        slots = slots_table.all(formula=formula)
        
        if slots:
            logger.info(f"Slot available: {slots[0]['id']}")
            return {
                "available": True,
                "slotId": slots[0]['id'],
                "date": data.date,
                "time": data.time,
                "duration": data.duration
            }
        else:
            logger.info(f"Slot not available for {data.date} at {data.time}")
            
            # Find alternative slots
            alternatives = await get_alternative_slots_internal(data.date)
            
            return {
                "available": False,
                "message": "Slot not available",
                "alternatives": alternatives[:5]  # Return top 5 alternatives
            }
            
    except Exception as e:
        logger.error(f"Error checking availability: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Get alternative slots (internal helper)
async def get_alternative_slots_internal(preferred_date: str) -> List[Dict]:
    """Get alternative available slots near the preferred date"""
    try:
        # Get slots within 7 days of preferred date
        formula = f"AND(IS_AFTER({{Date}}, TODAY()), {{Booked}}=FALSE())"
        slots = slots_table.all(formula=formula, sort=['Date'])
        
        alternatives = []
        for slot in slots[:10]:  # Get top 10
            alternatives.append({
                "date": slot['fields'].get('Date'),
                "time": slot['fields'].get('Time'),
                "slot_id": slot['id']
            })
        
        return alternatives
    except Exception as e:
        logger.error(f"Error getting alternatives: {str(e)}")
        return []

# Book appointment
@app.post("/book-appointment")
async def book_appointment(booking: BookingRequest):
    """
    Book an appointment
    
    Args:
        booking: BookingRequest object with appointment details
        
    Returns:
        JSON with booking confirmation
    """
    try:
        logger.info(f"Booking appointment for {booking.patient_name} on {booking.date}")
        
        # First check if slot is still available
        check_result = await check_availability(
            AvailabilityCheck(date=booking.date, time=booking.time, duration=booking.duration)
        )
        
        if not check_result['available']:
            raise HTTPException(status_code=400, detail="Slot no longer available")
        
        # Create appointment record
        appointment = appointments_table.create({
            "Patient Name": booking.patient_name,
            "Patient Phone": booking.patient_phone,
            "Appointment Date": booking.date,
            "Appointment Time": booking.time,
            "Duration": booking.duration,
            "Status": "Confirmed",
            "Booking Source": "AI Receptionist",
            "Reason": booking.reason or "General checkup",
            "Call ID": booking.call_id,
            "Created At": datetime.now().isoformat()
        })
        
        # Mark slot as booked
        slots_table.update(
            check_result['slotId'],
            {
                "Booked": True,
                "Booked By": booking.patient_phone,
                "Booked At": datetime.now().isoformat()
            }
        )
        
        logger.info(f"Successfully booked appointment: {appointment['id']}")
        
        return {
            "success": True,
            "appointmentId": appointment['id'],
            "date": booking.date,
            "time": booking.time,
            "message": f"Appointment confirmed for {booking.date} at {booking.time}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error booking appointment: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Send message to Vapi
@app.post("/send-to-vapi")
async def send_to_vapi(data: VapiResponse):
    """
    Send a message back to an active Vapi call
    
    Args:
        data: VapiResponse object with call ID, message, and function result
        
    Returns:
        JSON with send status
    """
    try:
        logger.info(f"Sending message to Vapi call: {data.callSid}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            vapi_url = f"https://api.vapi.ai/call/{data.callSid}/message"
            headers = {
                "Authorization": f"Bearer {os.getenv('VAPI_API_KEY')}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "message": data.message,
                "functionResult": data.functionResult
            }
            
            response = await client.post(vapi_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                logger.info(f"Successfully sent message to Vapi")
                return {"status": "sent", "vapi_response": response.json()}
            else:
                logger.error(f"Vapi API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=response.status_code, 
                    detail=f"Vapi API error: {response.text}"
                )
            
    except httpx.TimeoutException:
        logger.error("Timeout sending message to Vapi")
        raise HTTPException(status_code=504, detail="Timeout connecting to Vapi")
    except Exception as e:
        logger.error(f"Error sending to Vapi: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Log call
@app.post("/log-call")
async def log_call(call_log: CallLog, background_tasks: BackgroundTasks):
    """
    Log a call to the database
    
    Args:
        call_log: CallLog object with call details
        
    Returns:
        JSON with log status
    """
    try:
        background_tasks.add_task(log_call_to_airtable, call_log)
        return {"status": "logging", "call_id": call_log.call_id}
    except Exception as e:
        logger.error(f"Error logging call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def log_call_to_airtable(call_log: CallLog):
    """Background task to log call to Airtable"""
    try:
        logs_table.create({
            "Call ID": call_log.call_id,
            "Phone Number": call_log.phone_number,
            "Transcript": call_log.transcript,
            "Outcome": call_log.outcome,
            "Duration (seconds)": call_log.duration,
            "Timestamp": datetime.now().isoformat(),
            "Status": "Completed"
        })
        logger.info(f"Call logged: {call_log.call_id}")
    except Exception as e:
        logger.error(f"Failed to log call: {str(e)}")

# Get statistics
@app.get("/stats")
async def get_stats():
    """
    Get booking and call statistics
    
    Returns:
        JSON with various statistics
    """
    try:
        # Get total appointments
        all_appointments = appointments_table.all()
        total_appointments = len(all_appointments)
        
        # Get today's appointments
        today = datetime.now().strftime('%Y-%m-%d')
        today_formula = f"{{Appointment Date}}='{today}'"
        today_appointments = len(appointments_table.all(formula=today_formula))
        
        # Get AI-booked appointments
        ai_formula = "{{Booking Source}}='AI Receptionist'"
        ai_appointments = len(appointments_table.all(formula=ai_formula))
        
        # Get available slots
        available_formula = "{{Booked}}=FALSE()"
        available_slots = len(slots_table.all(formula=available_formula))
        
        return {
            "total_appointments": total_appointments,
            "today_appointments": today_appointments,
            "ai_booked_appointments": ai_appointments,
            "available_slots": available_slots,
            "status": "operational",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Get available slots for date range
@app.get("/slots")
async def get_available_slots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50
):
    """
    Get available appointment slots
    
    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        limit: Maximum number of slots to return
        
    Returns:
        List of available slots
    """
    try:
        if not start_date:
            start_date = datetime.now().strftime('%Y-%m-%d')
        
        if not end_date:
            end_date = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        
        formula = f"AND(IS_AFTER({{Date}}, '{start_date}'), IS_BEFORE({{Date}}, '{end_date}'), {{Booked}}=FALSE())"
        slots = slots_table.all(formula=formula, sort=['Date', 'Time'], max_records=limit)
        
        available_slots = []
        for slot in slots:
            available_slots.append({
                "slot_id": slot['id'],
                "date": slot['fields'].get('Date'),
                "time": slot['fields'].get('Time'),
                "duration": slot['fields'].get('Duration', 30)
            })
        
        return {
            "count": len(available_slots),
            "slots": available_slots,
            "date_range": {
                "start": start_date,
                "end": end_date
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting slots: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host=os.getenv("HOST", "0.0.0.0"), 
        port=int(os.getenv("PORT", 8000)),
        log_level=os.getenv("LOG_LEVEL", "info").lower()
    )
