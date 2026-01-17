import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import mysql.connector
from dotenv import load_dotenv
import logging

load_dotenv()

app = FastAPI(title="Smart Irrigation API")

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== CORS MIDDLEWARE ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== TIMEZONE CONFIGURATION ==========
TIMEZONE_OFFSET = 7  # WIB (UTC+7)

def get_local_time():
    """Get current time dengan timezone offset"""
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# ========== PYDANTIC MODELS ==========
class SensorData(BaseModel):
    moisture_level: float
    water_level: float
    pump_status: str = "OFF"

class ScheduleData(BaseModel):
    on_time: str
    off_time: str

class ControlUpdate(BaseModel):
    type: str  # "manual" atau "pause"
    target: Optional[str] = None  # "ON" atau "OFF" untuk manual
    minutes: Optional[int] = None  # durasi pause dalam menit

# ========== DATABASE CONNECTION ==========
def get_db_connection():
    """Connect to Railway MySQL"""
    try:
        db_host = os.getenv('MYSQLHOST')
        db_user = os.getenv('MYSQLUSER')
        db_pass = os.getenv('MYSQLPASSWORD')
        db_name = os.getenv('MYSQLDATABASE')
        db_port = os.getenv('MYSQLPORT', 3306)

        if not db_host:
            logger.error("❌ MYSQLHOST not found!")
            return None

        conn = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_pass,
            database=db_name,
            port=int(db_port),
            autocommit=True,
            connect_timeout=10
        )
        logger.info("✅ Database connected!")
        return conn
    except mysql.connector.Error as err:
        logger.error(f"❌ Database Error: {err}")
        return None

# ========== HELPER FUNCTIONS ==========

def parse_time_str(t: str) -> time:
    """Parse HH:MM atau HH:MM:SS to time object"""
    if not t:
        raise ValueError("empty time")
    parts = t.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        h, m = parts
        s = 0
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError("invalid time format")
    return time(h, m, s)

def is_now_between(on_str: str, off_str: str, now_dt: datetime) -> bool:
    """Check if current time is between on_time and off_time"""
    try:
        on_t = parse_time_str(on_str)
        off_t = parse_time_str(off_str)
    except Exception as e:
        logger.error(f"Error parsing time: {e}")
        return False
    
    now_t = now_dt.time()
    
    logger.info(f"Checking: {now_t} between {on_t} and {off_t}")
    
    if on_t <= off_t:
        # Normal case: 07:00 to 18:00
        return on_t <= now_t <= off_t
    else:
        # Overnight case: 22:00 to 06:00
        return now_t >= on_t or now_t <= off_t

def get_final_pump_status(db, now_dt: datetime) -> str:
    """
    Hitung status pompa final berdasarkan prioritas:
    1. Pause (jika aktif, pompa OFF)
    2. Manual Control (jika ON, pompa ON)
    3. Schedule (jika dalam jadwal, pompa ON)
    """
    target_status = "OFF"
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Priority 1: Check pause
        cursor.execute("""
            SELECT pause_until FROM pump_control 
            ORDER BY id DESC LIMIT 1
        """)
        ctrl = cursor.fetchone()
        
        if ctrl and ctrl['pause_until']:
            pause_dt = ctrl['pause_until']
            if now_dt < pause_dt:
                logger.info("⏸️  Pause active - pump OFF")
                cursor.close()
                return "OFF"
            else:
                logger.info("Pause expired - clearing")
                cursor.execute("""
                    UPDATE pump_control SET pause_until = NULL 
                    WHERE id = %s
                """, (ctrl['id'],))
        
        # Priority 2: Check manual control
        if ctrl:
            cursor.execute("""
                SELECT manual_target FROM pump_control 
                ORDER BY id DESC LIMIT 1
            """)
            ctrl = cursor.fetchone()
            
            if ctrl and ctrl['manual_target'] == 'ON':
                logger.info("🔧 Manual ON - pump ON")
                cursor.close()
                return "ON"
        
        # Priority 3: Check schedule
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE LIMIT 1
        """)
        sched = cursor.fetchone()
        
        if sched:
            logger.info(f"Schedule: {sched['on_time']} to {sched['off_time']}")
            if is_now_between(sched['on_time'], sched['off_time'], now_dt):
                logger.info("📅 Within schedule - pump ON")
                target_status = "ON"
            else:
                logger.info("Outside schedule - pump OFF")
                target_status = "OFF"
        else:
            logger.info("No schedule - pump OFF")
            target_status = "OFF"
        
        cursor.close()
        return target_status
    except Exception as e:
        logger.error(f"Error calculating pump status: {e}")
        return "OFF"

# ========== ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Smart Irrigation API - FastAPI",
        "version": "1.0"
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    db = get_db_connection()
    current_time = get_local_time()
    
    if db:
        db.close()
        return {
            "status": "healthy",
            "database": "connected",
            "server_time": current_time.isoformat(),
            "timezone": f"UTC+{TIMEZONE_OFFSET}"
        }
    return {
        "status": "unhealthy",
        "database": "disconnected",
        "server_time": current_time.isoformat()
    }

@app.get("/api/sensor/latest")
async def get_latest():
    """Get latest sensor data with pump status"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            ORDER BY created_at DESC LIMIT 1
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
            "moisture_level": 0,
            "water_level": 0,
            "pump_status": "OFF",
            "created_at": None
        }
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 20):
    """Get sensor history for 24-hour charts"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            ORDER BY created_at ASC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        if not rows:
            return []
        
        return [{
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level']),
            "pump_status": h['pump_status'],
            "time": h['created_at'].strftime("%H:%M") if h['created_at'] else "N/A"
        } for h in rows]
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor_data(data: SensorData):
    """
    Save sensor data from ESP32 dengan auto pump logic
    Response berisi command untuk ESP32
    """
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        now = get_local_time()
        logger.info(f"\n=== Sensor Save Called ===")
        logger.info(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Moisture: {data.moisture_level}%, Water: {data.water_level}%")
        
        # Hitung status pompa final
        target_status = get_final_pump_status(db, now)
        
        # Simpan sensor data ke database
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, target_status))
        cursor.close()
        
        logger.info(f"Final Pump Status: {target_status}")
        
        return {
            "status": "success",
            "command": target_status,
            "moisture": data.moisture_level,
            "water": data.water_level
        }
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(control: ControlUpdate):
    """Update pump control - manual ON/OFF atau pause"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get atau create control record
        cursor.execute("SELECT id FROM pump_control ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        
        if not row:
            cursor.execute("INSERT INTO pump_control (manual_target, pause_until) VALUES ('OFF', NULL)")
            last_id = cursor.lastrowid
        else:
            last_id = row['id']

        if control.type == "manual":
            # Manual control - clear pause
            target = (control.target.upper() if control.target else "OFF")
            logger.info(f"🔧 Manual control: {target}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = %s, pause_until = NULL 
                WHERE id = %s
            """, (target, last_id))
        
        elif control.type == "pause":
            # Pause control
            minutes = control.minutes or 0
            pause_until = get_local_time() + timedelta(minutes=minutes)
            
            logger.info(f"⏸️  Pause set: {minutes} minutes until {pause_until.strftime('%H:%M:%S')}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET pause_until = %s, manual_target = 'OFF' 
                WHERE id = %s
            """, (pause_until, last_id))
        
        cursor.close()
        return {"status": "success", "detail": f"{control.type} control updated"}
    except Exception as e:
        logger.error(f"❌ Error update_control: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(schedule: ScheduleData):
    """Add atau update pump schedule"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor()
        
        logger.info(f"Adding schedule: {schedule.on_time} to {schedule.off_time}")
        
        # Delete old schedules
        cursor.execute("DELETE FROM pump_schedules")
        
        # Insert new schedule
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (schedule.on_time, schedule.off_time))
        
        cursor.close()
        return {"status": "success", "message": "schedule added"}
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedules():
    """Get active schedules"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, on_time, off_time, is_active, created_at
            FROM pump_schedules 
            WHERE is_active = TRUE
        """)
        schedules = cursor.fetchall()
        cursor.close()
        
        if schedules:
            return [{
                "id": s['id'],
                "on_time": s['on_time'].strftime("%H:%M") if hasattr(s['on_time'], 'strftime') else str(s['on_time']),
                "off_time": s['off_time'].strftime("%H:%M") if hasattr(s['off_time'], 'strftime') else str(s['off_time']),
                "is_active": s['is_active']
            } for s in schedules]
        return []
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.delete("/api/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """Delete schedule"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor()
        cursor.execute("""
            UPDATE pump_schedules SET is_active = FALSE WHERE id = %s
        """, (schedule_id,))
        cursor.close()
        
        logger.info(f"Schedule {schedule_id} deleted")
        return {"status": "success", "message": "schedule deleted"}
    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()