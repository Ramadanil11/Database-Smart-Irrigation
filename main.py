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
        logger.error(f"❌ Error parsing time: {e}")
        return False
    
    now_t = now_dt.time()
    
    logger.info(f"📅 Schedule check: {now_t} between {on_t} and {off_t}")
    
    if on_t <= off_t:
        # Normal case: 07:00 to 18:00
        result = on_t <= now_t <= off_t
    else:
        # Overnight case: 22:00 to 06:00
        result = now_t >= on_t or now_t <= off_t
    
    logger.info(f"📅 Schedule result: {result}")
    return result

def get_final_pump_status(db, now_dt: datetime) -> str:
    """
    Hitung status pompa final berdasarkan prioritas:
    1. Pause (jika aktif, pompa OFF) - HIGHEST PRIORITY
    2. Manual Control (jika ON, pompa ON) - MEDIUM PRIORITY
    3. Schedule (jika dalam jadwal, pompa ON) - LOWEST PRIORITY
    """
    target_status = "OFF"
    reason = "Default OFF"
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # ========== PRIORITY 1: CHECK PAUSE ==========
        logger.info(f"\n🔍 [PRIORITY 1] Checking PAUSE...")
        cursor.execute("""
            SELECT id, pause_until FROM pump_control 
            ORDER BY id DESC LIMIT 1
        """)
        ctrl = cursor.fetchone()
        
        if ctrl:
            logger.info(f"   Control record found: {ctrl}")
            
            if ctrl['pause_until']:
                pause_dt = ctrl['pause_until']
                logger.info(f"   Pause until: {pause_dt}, Now: {now_dt}")
                
                if now_dt < pause_dt:
                    logger.info(f"✋ PAUSE ACTIVE - Returning OFF")
                    cursor.close()
                    return "OFF"
                else:
                    logger.info(f"✓ Pause expired, clearing...")
                    cursor.execute("""
                        UPDATE pump_control SET pause_until = NULL 
                        WHERE id = %s
                    """, (ctrl['id'],))
        
        # ========== PRIORITY 2: CHECK MANUAL CONTROL ==========
        logger.info(f"\n🔍 [PRIORITY 2] Checking MANUAL control...")
        cursor.execute("""
            SELECT id, manual_target FROM pump_control 
            ORDER BY id DESC LIMIT 1
        """)
        ctrl = cursor.fetchone()
        
        if ctrl and ctrl['manual_target']:
            logger.info(f"   Manual target: {ctrl['manual_target']}")
            
            if ctrl['manual_target'] == 'ON':
                logger.info(f"🔧 MANUAL ON detected - Returning ON")
                target_status = "ON"
                reason = "Manual ON"
                cursor.close()
                return target_status
            elif ctrl['manual_target'] == 'OFF':
                logger.info(f"🔧 MANUAL OFF detected - Returning OFF")
                cursor.close()
                return "OFF"
        
        # ========== PRIORITY 3: CHECK SCHEDULE ==========
        logger.info(f"\n🔍 [PRIORITY 3] Checking SCHEDULE...")
        cursor.execute("""
            SELECT id, on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE 
            ORDER BY id DESC LIMIT 1
        """)
        sched = cursor.fetchone()
        
        if sched:
            on_time_str = sched['on_time']
            off_time_str = sched['off_time']
            
            # Handle both time and datetime objects
            if hasattr(on_time_str, 'strftime'):
                on_time_str = on_time_str.strftime("%H:%M:%S")
            if hasattr(off_time_str, 'strftime'):
                off_time_str = off_time_str.strftime("%H:%M:%S")
            
            logger.info(f"   Schedule: {on_time_str} to {off_time_str}")
            
            if is_now_between(on_time_str, off_time_str, now_dt):
                logger.info(f"📅 WITHIN SCHEDULE - Returning ON")
                target_status = "ON"
                reason = "Schedule active"
            else:
                logger.info(f"📅 OUTSIDE SCHEDULE - Returning OFF")
                target_status = "OFF"
                reason = "Outside schedule"
        else:
            logger.info(f"   No schedule found")
            target_status = "OFF"
            reason = "No schedule"
        
        cursor.close()
        logger.info(f"\n✅ FINAL STATUS: {target_status} ({reason})\n")
        return target_status
        
    except Exception as e:
        logger.error(f"❌ Error calculating pump status: {e}")
        import traceback
        traceback.print_exc()
        return "OFF"

# ========== ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Smart Irrigation API - FastAPI",
        "version": "2.0",
        "timestamp": get_local_time().isoformat()
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
        logger.error(f"❌ Error in get_latest: {e}")
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
        logger.error(f"❌ Error in get_history: {e}")
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
        logger.info(f"\n{'='*50}")
        logger.info(f"📨 SENSOR DATA RECEIVED")
        logger.info(f"{'='*50}")
        logger.info(f"⏰ Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"💧 Moisture: {data.moisture_level}%")
        logger.info(f"🌊 Water: {data.water_level}%")
        
        # Hitung status pompa final
        target_status = get_final_pump_status(db, now)
        
        # Simpan sensor data ke database
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, target_status))
        cursor.close()
        
        logger.info(f"💾 Sensor data saved")
        logger.info(f"🔌 Command to ESP32: {target_status}")
        logger.info(f"{'='*50}\n")
        
        return {
            "status": "success",
            "command": target_status,
            "moisture": data.moisture_level,
            "water": data.water_level,
            "timestamp": now.isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Error in save_sensor_data: {e}")
        import traceback
        traceback.print_exc()
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
        logger.info(f"\n{'='*50}")
        logger.info(f"🎮 CONTROL UPDATE RECEIVED")
        logger.info(f"{'='*50}")
        logger.info(f"Type: {control.type}")
        
        cursor = db.cursor(dictionary=True)
        
        # Get atau create control record
        cursor.execute("SELECT id FROM pump_control ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        
        if not row:
            logger.info("Creating new pump_control record...")
            cursor.execute("INSERT INTO pump_control (manual_target, pause_until) VALUES ('OFF', NULL)")
            last_id = cursor.lastrowid
        else:
            last_id = row['id']

        if control.type == "manual":
            # Manual control - RESET PAUSE dan set manual target
            target = (control.target.upper() if control.target else "OFF")
            logger.info(f"🔧 Setting MANUAL control to: {target}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = %s, pause_until = NULL 
                WHERE id = %s
            """, (target, last_id))
            
            logger.info(f"✅ Manual control SET: {target}")
        
        elif control.type == "pause":
            # Pause control - set pause_until dan reset manual
            minutes = control.minutes or 0
            pause_until = get_local_time() + timedelta(minutes=minutes)
            
            logger.info(f"⏸️  Setting PAUSE: {minutes} minutes")
            logger.info(f"   Until: {pause_until.strftime('%Y-%m-%d %H:%M:%S')}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET pause_until = %s, manual_target = 'OFF' 
                WHERE id = %s
            """, (pause_until, last_id))
            
            logger.info(f"✅ Pause SET for {minutes} minutes")
        
        cursor.close()
        logger.info(f"{'='*50}\n")
        
        return {
            "status": "success",
            "detail": f"{control.type} control updated",
            "timestamp": get_local_time().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Error in update_control: {e}")
        import traceback
        traceback.print_exc()
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
        logger.info(f"\n{'='*50}")
        logger.info(f"📅 SCHEDULE ADD/UPDATE RECEIVED")
        logger.info(f"{'='*50}")
        logger.info(f"ON Time: {schedule.on_time}")
        logger.info(f"OFF Time: {schedule.off_time}")
        
        cursor = db.cursor()
        
        # Delete old schedules
        cursor.execute("DELETE FROM pump_schedules")
        logger.info("Old schedules cleared")
        
        # Insert new schedule
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (schedule.on_time, schedule.off_time))
        
        cursor.close()
        logger.info(f"✅ New schedule SAVED")
        logger.info(f"   ON: {schedule.on_time} → OFF: {schedule.off_time}")
        logger.info(f"{'='*50}\n")
        
        return {
            "status": "success",
            "message": "schedule added",
            "on_time": schedule.on_time,
            "off_time": schedule.off_time,
            "timestamp": get_local_time().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Error in add_schedule: {e}")
        import traceback
        traceback.print_exc()
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
            result = []
            for s in schedules:
                on_time = s['on_time']
                off_time = s['off_time']
                
                if hasattr(on_time, 'strftime'):
                    on_time = on_time.strftime("%H:%M:%S")
                else:
                    on_time = str(on_time)
                
                if hasattr(off_time, 'strftime'):
                    off_time = off_time.strftime("%H:%M:%S")
                else:
                    off_time = str(off_time)
                
                result.append({
                    "id": s['id'],
                    "on_time": on_time,
                    "off_time": off_time,
                    "is_active": s['is_active']
                })
            return result
        return []
    except Exception as e:
        logger.error(f"❌ Error in get_schedules: {e}")
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
        
        logger.info(f"📅 Schedule {schedule_id} deactivated")
        return {
            "status": "success",
            "message": "schedule deleted",
            "timestamp": get_local_time().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Error in delete_schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ========== DEBUG ENDPOINT ==========
@app.get("/api/debug/control-status")
async def debug_control_status():
    """Debug endpoint - Lihat current control status"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM pump_control ORDER BY id DESC LIMIT 1")
        control = cursor.fetchone()
        
        cursor.execute("SELECT * FROM pump_schedules WHERE is_active = TRUE")
        schedule = cursor.fetchone()
        
        cursor.close()
        
        return {
            "control": control,
            "schedule": schedule,
            "current_time": get_local_time().isoformat(),
            "calculated_status": get_final_pump_status(db, get_local_time()) if db else "Unknown"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()