import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import mysql.connector
from dotenv import load_dotenv
import logging

load_dotenv()

app = FastAPI(title="Smart Irrigation API v3")

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== CORS MIDDLEWARE ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== TIMEZONE ==========
TIMEZONE_OFFSET = 7

def get_local_time():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# ========== MODELS ==========
class SensorData(BaseModel):
    moisture_level: float
    water_level: float

class ScheduleData(BaseModel):
    on_time: str  # "HH:MM:SS"
    off_time: str  # "HH:MM:SS"

class ControlUpdate(BaseModel):
    action: str  # "MANUAL_ON", "MANUAL_OFF", "AUTO", "PAUSE"
    minutes: Optional[int] = None  # Untuk PAUSE

# ========== DATABASE ==========
def get_db():
    try:
        return mysql.connector.connect(
            host=os.getenv('MYSQLHOST'),
            user=os.getenv('MYSQLUSER'),
            password=os.getenv('MYSQLPASSWORD'),
            database=os.getenv('MYSQLDATABASE'),
            port=int(os.getenv('MYSQLPORT', 3306)),
            autocommit=True
        )
    except Exception as e:
        logger.error(f"❌ DB Error: {e}")
        return None

# ========== HELPER FUNCTIONS ==========

def parse_time(time_str: str) -> time:
    """Parse HH:MM:SS to time object"""
    parts = time_str.split(':')
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

def is_in_schedule(now_dt: datetime, on_str: str, off_str: str) -> bool:
    """Check if current time is within schedule"""
    on_t = parse_time(on_str)
    off_t = parse_time(off_str)
    now_t = now_dt.time()
    
    if on_t <= off_t:
        # Normal: 07:00 to 18:00
        result = on_t <= now_t <= off_t
    else:
        # Overnight: 22:00 to 06:00
        result = now_t >= on_t or now_t <= off_t
    
    logger.info(f"  Schedule check: {now_t} in {on_t}-{off_t} = {result}")
    return result

def calculate_pump_status(db, now_dt: datetime) -> str:
    """
    LOGIC POMPA FINAL:
    1. Jika PAUSE aktif → OFF
    2. Jika MANUAL_ON → ON
    3. Jika MANUAL_OFF → OFF
    4. Jika AUTO → check schedule
    5. Default → OFF
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"🔄 CALCULATING PUMP STATUS at {now_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get current control state
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        if not control:
            logger.warning("⚠️  No control record found!")
            return "OFF"
        
        logger.info(f"\n📊 Control State:")
        logger.info(f"   manual_mode: {control['manual_mode']}")
        logger.info(f"   pause_end_time: {control['pause_end_time']}")
        
        # PRIORITY 1: Check if PAUSE is active
        if control['pause_end_time']:
            pause_dt = control['pause_end_time']
            logger.info(f"\n⏸️  [PRIORITY 1] PAUSE CHECK:")
            logger.info(f"   Pause until: {pause_dt}")
            logger.info(f"   Now: {now_dt}")
            
            if now_dt < pause_dt:
                logger.info(f"   ✓ PAUSE ACTIVE → Return OFF")
                cursor.close()
                return "OFF"
            else:
                logger.info(f"   ✓ Pause expired, clearing...")
                cursor.execute("""
                    UPDATE pump_control SET pause_end_time = NULL WHERE id = 1
                """)
        
        # PRIORITY 2: Check MANUAL mode
        logger.info(f"\n🔧 [PRIORITY 2] MANUAL MODE CHECK:")
        logger.info(f"   manual_mode: {control['manual_mode']}")
        
        if control['manual_mode'] == 'MANUAL_ON':
            logger.info(f"   ✓ MANUAL_ON → Return ON")
            cursor.close()
            return "ON"
        elif control['manual_mode'] == 'MANUAL_OFF':
            logger.info(f"   ✓ MANUAL_OFF → Return OFF")
            cursor.close()
            return "OFF"
        
        # PRIORITY 3: Check AUTO (Schedule)
        logger.info(f"\n📅 [PRIORITY 3] AUTO MODE (SCHEDULE) CHECK:")
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE LIMIT 1
        """)
        schedule = cursor.fetchone()
        
        if schedule:
            on_time = schedule['on_time']
            off_time = schedule['off_time']
            
            # Convert to string if needed
            if hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            if hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            logger.info(f"   Schedule found: {on_time} - {off_time}")
            
            if is_in_schedule(now_dt, on_time, off_time):
                logger.info(f"   ✓ Within schedule → Return ON")
                cursor.close()
                return "ON"
            else:
                logger.info(f"   ✓ Outside schedule → Return OFF")
                cursor.close()
                return "OFF"
        else:
            logger.info(f"   No schedule found → Return OFF")
            cursor.close()
            return "OFF"
    
    except Exception as e:
        logger.error(f"❌ Error calculating status: {e}")
        return "OFF"

# ========== ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "status": "online",
        "version": "3.0",
        "timestamp": get_local_time().isoformat()
    }

@app.get("/health")
async def health():
    db = get_db()
    if db:
        db.close()
        return {
            "status": "healthy",
            "database": "connected",
            "server_time": get_local_time().isoformat(),
            "timezone": f"UTC+{TIMEZONE_OFFSET}"
        }
    return {"status": "unhealthy", "database": "disconnected"}

@app.get("/api/sensor/latest")
async def get_latest():
    """Get latest sensor data"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            return {
                "moisture_level": float(row['moisture_level']),
                "water_level": float(row['water_level']),
                "pump_status": row['pump_status'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None
            }
        
        return {
            "moisture_level": 0.0,
            "water_level": 0.0,
            "pump_status": "OFF",
            "created_at": None
        }
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 100):
    """Get sensor history for 24 hours"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            ORDER BY created_at ASC LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        return [{
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level']),
            "pump_status": h['pump_status'],
            "time": h['created_at'].strftime("%H:%M") if h['created_at'] else ""
        } for h in rows]
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor(data: SensorData):
    """
    ESP32 sends sensor data
    API calculates pump status and returns command
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        logger.info(f"\n📨 SENSOR DATA RECEIVED")
        logger.info(f"   Moisture: {data.moisture_level}%")
        logger.info(f"   Water: {data.water_level}%")
        
        # Calculate final pump status
        pump_status = calculate_pump_status(db, now)
        
        # Save to database
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, pump_status))
        cursor.close()
        
        logger.info(f"💾 Saved to DB: pump_status = {pump_status}")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "command": pump_status,
            "timestamp": now.isoformat()
        }
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(update: ControlUpdate):
    """
    Update pump control:
    - MANUAL_ON: Force pompa ON (ignore schedule)
    - MANUAL_OFF: Force pompa OFF
    - AUTO: Follow schedule
    - PAUSE: Pause sistem untuk N menit
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🎮 CONTROL UPDATE: {action}")
        logger.info(f"{'='*60}")
        
        cursor = db.cursor()
        
        if action == "PAUSE":
            # Pause untuk N menit
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            logger.info(f"⏸️  Pausing for {minutes} minutes")
            logger.info(f"   Until: {pause_until.strftime('%H:%M:%S')}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            result_msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            logger.info(f"🔌 Setting MANUAL_ON")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Manual ON"
        
        elif action == "MANUAL_OFF":
            logger.info(f"🔌 Setting MANUAL_OFF")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Manual OFF"
        
        elif action == "AUTO":
            logger.info(f"📅 Setting AUTO mode (schedule)")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Auto mode (schedule)"
        
        else:
            return {"status": "error", "detail": "Invalid action"}
        
        cursor.close()
        
        logger.info(f"✅ {result_msg}")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "action": action,
            "message": result_msg,
            "timestamp": now.isoformat()
        }
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(data: ScheduleData):
    """Add or update schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"📅 SCHEDULE UPDATE")
        logger.info(f"   ON: {data.on_time}")
        logger.info(f"   OFF: {data.off_time}")
        
        cursor = db.cursor()
        
        # Delete old schedules
        cursor.execute("DELETE FROM pump_schedules")
        
        # Insert new
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (data.on_time, data.off_time))
        
        cursor.close()
        
        logger.info(f"✅ Schedule saved")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "on_time": data.on_time,
            "off_time": data.off_time
        }
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedule():
    """Get current schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT on_time, off_time, is_active 
            FROM pump_schedules WHERE is_active = TRUE LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            on_time = row['on_time']
            off_time = row['off_time']
            
            if hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            if hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            return {
                "on_time": on_time,
                "off_time": off_time,
                "is_active": True
            }
        
        return {"on_time": None, "off_time": None, "is_active": False}
    
    finally:
        db.close()

@app.get("/api/control/status")
async def get_control_status():
    """Get current control state and calculated pump status"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        cursor.close()
        
        if not control:
            return {"status": "error", "detail": "No control record"}
        
        # Calculate what pump SHOULD be
        calculated_status = calculate_pump_status(db, now)
        
        return {
            "manual_mode": control['manual_mode'],
            "pause_end_time": control['pause_end_time'].isoformat() if control['pause_end_time'] else None,
            "calculated_pump_status": calculated_status,
            "server_time": now.isoformat()
        }
    
    finally:
        db.close()